# -*- coding: utf-8 -*-
"""端到端验证：逐条核对 14 项预期结果。

参考合成器在本文件内独立实现（与 src/compositor.py 不共享代码），
同样采用整数 source-over 公式，用于核对成品像素；另用浮点定义式
抽查不透明度 0.5 的混合比例。全部通过时退出码为 0。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src", "compositor.py")
INPUT = os.path.join(ROOT, "samples", "input")
WORK = os.path.join(ROOT, "samples", "verify_work")

passed = 0
failed: list[str] = []


def check(condition: bool, title: str, detail: str = "") -> None:
    if condition:
        global passed
        passed += 1
        print(f"  [PASS] {title}")
    else:
        failed.append(f"{title} {detail}".strip())
        print(f"  [FAIL] {title} {detail}")


def run_compositor(desc_name: str, out_name: str) -> subprocess.CompletedProcess:
    out_dir = os.path.join(WORK, out_name)
    cmd = [sys.executable, "-I", SRC, os.path.join(INPUT, desc_name), out_dir]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")


def read_record(out_name: str) -> dict:
    with open(os.path.join(WORK, out_name, "record.json"), encoding="utf-8") as fp:
        return json.load(fp)


def read_image(out_name: str) -> Image.Image:
    return Image.open(os.path.join(WORK, out_name, "composite.png")).convert("RGBA")


def reference_composite(desc_path: str) -> Image.Image:
    """独立参考实现：与合成器相同的整数数学，但代码路径完全独立。"""
    with open(desc_path, encoding="utf-8") as fp:
        desc = json.load(fp)
    w, h = desc["canvas_width"], desc["canvas_height"]
    bg_text = desc["background"].lstrip("#")
    bg_text = bg_text + "ff" if len(bg_text) == 6 else bg_text
    bg = tuple(int(bg_text[i:i + 2], 16) for i in range(0, 8, 2))
    canvas = Image.new("RGBA", (w, h), bg)
    px = canvas.load()
    base = os.path.dirname(desc_path)
    for layer in desc["layers"]:
        op = layer.get("opacity")
        if not isinstance(op, (int, float)) or not (0 <= op <= 1):
            continue
        path = os.path.join(base, layer["file"])
        if not os.path.isfile(path):
            continue
        try:
            src = Image.open(path).convert("RGBA")
            src.load()
        except Exception:
            continue
        spx = src.load()
        ox, oy = layer["x"], layer["y"]
        for cy in range(max(oy, 0), min(oy + src.height, h)):
            for cx in range(max(ox, 0), min(ox + src.width, w)):
                r, g, b, a = spx[cx - ox, cy - oy]
                sa = min(255, int(a * op + 0.5))
                if sa == 0:
                    continue
                dr, dg, db, da = px[cx, cy]
                remain = 255 - sa
                oa = sa + (da * remain + 127) // 255
                or_ = (r * sa + (dr * da * remain + 127) // 255 + oa // 2) // oa
                og = (g * sa + (dg * da * remain + 127) // 255 + oa // 2) // oa
                ob = (b * sa + (db * da * remain + 127) // 255 + oa // 2) // oa
                px[cx, cy] = (or_, og, ob, oa)
    return canvas


def images_equal(a: Image.Image, b: Image.Image) -> bool:
    return a.size == b.size and list(a.getdata()) == list(b.getdata())


def main() -> int:
    if os.path.isdir(WORK):
        shutil.rmtree(WORK)
    os.makedirs(WORK, exist_ok=True)

    # (1) 合法描述与齐全图层 -> 成品 PNG + 记录 JSON。
    print("(1) 合法描述正常产出")
    r = run_compositor("case1_valid.json", "t1")
    check(r.returncode == 0, "退出码为 0", r.stderr)
    check(os.path.isfile(os.path.join(WORK, "t1", "composite.png")), "成品 PNG 已生成")
    check(os.path.isfile(os.path.join(WORK, "t1", "record.json")), "记录 JSON 已生成")

    # (2) 自下而上顺序叠加，与逐层手工合成像素一致。
    print("(2) 叠加顺序与独立参考实现逐像素一致")
    got = read_image("t1")
    ref = reference_composite(os.path.join(INPUT, "case1_valid.json"))
    check(images_equal(got, ref), "三层半透明叠加与手工参考像素相同")

    # (3) 不透明度与自身 alpha 做乘积混合：用 0.5 不透明度的构造场景抽查。
    print("(3) 不透明度 × 自身 alpha 的乘积混合")
    # 探针 A：透明底色 -> 成品 alpha 直接等于有效 alpha，源颜色被保留。
    probe_dir = os.path.join(WORK, "probe")
    os.makedirs(probe_dir, exist_ok=True)
    layer = Image.new("RGBA", (1, 1), (255, 0, 0, 128))
    layer_path = os.path.join(probe_dir, "p.png")
    layer.save(layer_path)
    desc = {
        # 全透明底色：成品 alpha 直接反映图层的有效 alpha。
        "canvas_width": 1, "canvas_height": 1, "background": "#00000000",
        "layers": [{"name": "p", "file": layer_path, "x": 0, "y": 0, "opacity": 0.5}],
    }
    desc_path = os.path.join(probe_dir, "d.json")
    with open(desc_path, "w", encoding="utf-8") as fp:
        json.dump(desc, fp)
    subprocess.run([sys.executable, "-I", SRC, desc_path, probe_dir], check=True)
    out = Image.open(os.path.join(probe_dir, "composite.png")).convert("RGBA")
    pr, pg, pb, pa = out.getpixel((0, 0))
    # 有效 alpha = 128*0.5 = 64；目标透明时源颜色保留为 255。
    check(pa == 64 and pr == 255 and pg == 0 and pb == 0,
          "透明底上 alpha=128 且 opacity=0.5：有效 alpha=64、源颜色保留",
          f"实际=({pr},{pg},{pb},{pa})")

    # 探针 B：不透明黑底 -> 颜色按有效 alpha 的混合比例衰减（红=64）。
    probe_b = os.path.join(WORK, "probe_b")
    os.makedirs(probe_b, exist_ok=True)
    layer_b_path = os.path.join(probe_b, "p.png")
    layer.save(layer_b_path)
    desc_b = {
        "canvas_width": 1, "canvas_height": 1, "background": "#000000",
        "layers": [{"name": "p", "file": layer_b_path, "x": 0, "y": 0, "opacity": 0.5}],
    }
    desc_b_path = os.path.join(probe_b, "d.json")
    with open(desc_b_path, "w", encoding="utf-8") as fp:
        json.dump(desc_b, fp)
    subprocess.run([sys.executable, "-I", SRC, desc_b_path, probe_b], check=True)
    out_b = Image.open(os.path.join(probe_b, "composite.png")).convert("RGBA")
    br, bg, bb, ba = out_b.getpixel((0, 0))
    # 不透明底上成品 alpha=255；红 = 255*(64/255) = 64，体现 0.5 的混合比例。
    check(ba == 255 and br == 64 and bg == 0 and bb == 0,
          "不透明底上 opacity=0.5：红色按混合比例变为 64",
          f"实际=({br},{bg},{bb},{ba})")

    # (4) 缺文件跳过该层，其余继续；记录列为未完成并带原因。
    print("(4) 图层缺失跳过且成品仍生成")
    r = run_compositor("case2_missing_file.json", "t4")
    check(r.returncode == 0, "退出码为 0", r.stderr)
    rec = read_record("t4")
    bad = [x for x in rec["incomplete_layers"] if x["name"] == "中层-半透明绿"]
    check(len(bad) == 1 and bad[0]["file"] is None and bad[0]["reason"],
          "缺失层列入未完成、file=null 且含原因")
    check(rec["layer_counts"]["completed"] == 2, "其余两层照常完成")
    check(os.path.isfile(os.path.join(WORK, "t4", "composite.png")), "成品仍生成")

    # (5) 小尺寸图层按坐标放置不拉伸，区域外保留底色。
    print("(5) 小图层角落放置，外部保留底色")
    r = run_compositor("case4_placement.json", "t5")
    img = read_image("t5")
    bg = (0x2b, 0x3a, 0x55, 255)
    # 角落 60x40 之外且不被负坐标图层覆盖的点必须仍是底色。
    check(img.getpixel((230, 170)) == bg, "右下角保持画布底色")
    check(img.getpixel((100, 20)) == bg, "小图层矩形外保持画布底色")
    check(img.getpixel((5, 5)) != bg, "小图层覆盖的角落像素已改变")

    # (6) 越界裁剪：负坐标不报错，成品尺寸等于画布，越界像素不出现。
    print("(6) 坐标越界裁剪")
    check(r.returncode == 0, "负坐标运行不报错", r.stderr)
    check(img.size == (240, 180), "成品尺寸严格等于画布尺寸", str(img.size))
    rec5 = read_record("t5")
    full_out = [x for x in rec5["completed_layers"] if x["name"] == "完全越界图层"]
    check(len(full_out) == 1 and full_out[0]["mixed_pixels"] == 0,
          "完全越界层保留在已完成列表但混合像素为 0")

    # (7) 非法底色：无成品、提示底色非法、正常退出。
    print("(7) 非法底色使整份描述不可用")
    for idx, case in enumerate(("case6_bad_background.json", "case7_bad_background2.json"), start=1):
        r = run_compositor(case, f"t7_{idx}")
        no_output = not os.path.exists(os.path.join(WORK, f"t7_{idx}"))
        check(r.returncode == 0 and "底色格式非法" in r.stderr and no_output,
              f"非法底色 {case}：无成品且提示非法")

    # (8) 不透明度缺失/越界跳过，其余继续。
    print("(8) 不透明度非法层跳过")
    r = run_compositor("case3_bad_opacity.json", "t8")
    rec = read_record("t8")
    names = {x["name"]: x for x in rec["incomplete_layers"]}
    check("底层-红" in names and "1.5" in names["底层-红"]["reason"] or
          "范围" in names.get("底层-红", {}).get("reason", ""),
          "opacity=1.5 的层列为未完成")
    check("顶层-半透明蓝" in names, "opacity 缺失的层列为未完成")
    check(rec["layer_counts"]["completed"] == 1, "合法层继续完成合成")

    # (9) 成品尺寸严格等于画布，不随图层尺寸变化。
    print("(9) 成品尺寸恒等于画布")
    check(read_image("t1").size == (240, 180), "case1 成品尺寸为 240x180")
    check(read_image("t4").size == (240, 180), "缺层场景成品尺寸仍为 240x180")

    # (10) 记录分列已完成/未完成，未完成 file=null 且含原因。
    print("(10) 记录 JSON 结构")
    rec = read_record("t4")
    ok = all(x.get("file") and x["name"] and "x" in x and "y" in x
             for x in rec["completed_layers"])
    bad_ok = all(x.get("file") is None and x.get("reason") and x.get("name")
                 for x in rec["incomplete_layers"])
    check(ok and bad_ok and len(rec["completed_layers"]) == 2
          and len(rec["incomplete_layers"]) == 1,
          "已完成/未完成分列，未完成条目 file=null 且含名称、坐标、原因")

    # (11) 全部图层未完成：纯底色成品 + 无可用图层说明。
    print("(11) 全部缺失时输出纯底色成品")
    r = run_compositor("case9_all_missing.json", "t11")
    img = read_image("t11")
    flat = all(p == (0x2b, 0x3a, 0x55, 255) for p in img.getdata())
    rec = read_record("t11")
    check(flat, "成品每个像素均为画布底色")
    check(rec["layer_counts"]["completed"] == 0 and "无可用图层" in rec["status"],
          "记录标明无可用图层")

    # (12) 重复运行逐字节一致，记录无易变字段。
    print("(12) 确定性：两次运行逐字节一致")
    run_compositor("case1_valid.json", "t12_a")
    run_compositor("case1_valid.json", "t12_b")
    for fname in ("composite.png", "record.json"):
        with open(os.path.join(WORK, "t12_a", fname), "rb") as fa, \
             open(os.path.join(WORK, "t12_b", fname), "rb") as fb:
            check(fa.read() == fb.read(), f"{fname} 两次运行逐字节一致")
    rec_text = open(os.path.join(WORK, "t12_a", "record.json"), encoding="utf-8").read()
    volatile = [k for k in ("time", "timestamp", "date", "pid") if k in rec_text.lower()]
    check(not volatile, "记录不含易变字段", str(volatile))

    # (13) CLI 入口 + 不存在的嵌套输出目录自动创建并按约定落位。
    print("(13) 命令行入口与嵌套目录创建")
    nested = os.path.join(WORK, "t13", "deep", "nested", "out")
    cmd = [sys.executable, "-I", SRC, os.path.join(INPUT, "case1_valid.json"), nested]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    check(r.returncode == 0 and os.path.isdir(nested), "嵌套输出目录被创建")
    check(os.path.isfile(os.path.join(nested, "composite.png"))
          and os.path.isfile(os.path.join(nested, "record.json")),
          "成品与记录按约定文件名落位")

    # (14) 描述缺失/损坏：不崩溃、正常退出、提示不可用、无成品。
    print("(14) 描述缺失或损坏时优雅退出")
    r = run_compositor("case10_corrupted.json", "t14a")
    check(r.returncode == 0 and "描述不可用" in r.stderr
          and not os.path.exists(os.path.join(WORK, "t14a")),
          "损坏 JSON：正常结束、提示不可用、无成品")
    r = run_compositor("does_not_exist.json", "t14b")
    check(r.returncode == 0 and "描述不可用" in r.stderr
          and not os.path.exists(os.path.join(WORK, "t14b")),
          "缺失描述文件：正常结束、提示不可用、无成品")

    # 附加：8 位底色合法可合成，且与独立参考一致。
    print("附加: 8 位十六进制底色")
    r = run_compositor("case8_background_8digit.json", "tbg8")
    check(r.returncode == 0, "8 位底色描述可运行", r.stderr)
    got = read_image("tbg8")
    ref = reference_composite(os.path.join(INPUT, "case8_background_8digit.json"))
    check(images_equal(got, ref), "8 位底色成品与参考实现一致")

    # 附加：损坏 PNG 跳过。
    print("附加: 无法解码的 PNG 跳过")
    r = run_compositor("case5_broken_png.json", "tbroken")
    rec = read_record("tbroken")
    check(any(x["name"] == "顶层-半透明蓝" and "无法解码" in x["reason"]
              for x in rec["incomplete_layers"]),
          "坏 PNG 列为未完成并说明无法解码")
    check(rec["layer_counts"]["completed"] == 2, "其余两层照常完成")

    print(f"\n结果：{passed} 项通过，{len(failed)} 项失败")
    if failed:
        for item in failed:
            print(" -", item)
        return 1
    print("全部验证通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
