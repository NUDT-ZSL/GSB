# -*- coding: utf-8 -*-
"""确定性生成业务样例：含半透明像素的图层 PNG 与对应描述 JSON。

不使用随机数，所有像素由坐标公式决定，任何机器、任何时间重复生成
结果逐字节一致。生成内容：

  samples/input/
    layer_bottom.png   底层：左半幅不透明红渐变 + 右半幅半透明
    layer_middle.png   中层：半透明绿方块（验证透明度乘积混合）
    layer_top.png      顶层：偏移放置的半透明蓝条（验证顺序与越界裁剪）
    layer_corner.png   小尺寸图层（验证按坐标放置、不拉伸）
    broken.png         伪装成 PNG 的损坏文件（验证无法解码跳过）
    case_*.json        覆盖正常/缺文件/坏文件/不透明度非法/坐标越界/
                       底色非法/全缺失/损坏描述 等场景的描述
"""

from __future__ import annotations

import json
import os

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
INPUT_DIR = os.path.join(HERE, "input")

CANVAS_W, CANVAS_H = 240, 180
BACKGROUND = "#2b3a55"


def _save(img: Image.Image, name: str) -> str:
    path = os.path.join(INPUT_DIR, name)
    img.save(path, format="PNG", compress_level=6)
    return path


def make_bottom() -> None:
    """底层 200x140：左半幅不透明红色渐变，右半幅 alpha=128 的红色。"""
    img = Image.new("RGBA", (200, 140))
    px = img.load()
    for y in range(140):
        for x in range(200):
            if x < 100:
                px[x, y] = (200, 40 + (x + y) % 60, 40, 255)
            else:
                px[x, y] = (200, 60, 60, 128)
    _save(img, "layer_bottom.png")


def make_middle() -> None:
    """中层 120x100：半透明绿色方块，alpha 随列变化（含多种半透明值）。"""
    img = Image.new("RGBA", (120, 100))
    px = img.load()
    for y in range(100):
        for x in range(120):
            px[x, y] = (30, 190, 70, 80 + (x % 8) * 20)  # alpha: 80..220
    _save(img, "layer_middle.png")


def make_top() -> None:
    """顶层 100x60：半透明蓝条，放在偏移位置（局部越界场景使用）。"""
    img = Image.new("RGBA", (100, 60))
    px = img.load()
    for y in range(60):
        for x in range(100):
            px[x, y] = (50, 90, 220, 160)
    _save(img, "layer_top.png")


def make_corner() -> None:
    """小尺寸图层 60x40：半透明青色，用于角落放置，不允许拉伸。"""
    img = Image.new("RGBA", (60, 40))
    px = img.load()
    for y in range(40):
        for x in range(60):
            px[x, y] = (40, 210, 210, 200)
    _save(img, "layer_corner.png")


def make_broken() -> None:
    """内容不是合法 PNG 编码的“图层文件”。"""
    with open(os.path.join(INPUT_DIR, "broken.png"), "wb") as fp:
        fp.write(b"\x89PNG\r\n\x1a\nthis is not a real png payload")


def _write_json(name: str, payload: dict) -> str:
    path = os.path.join(INPUT_DIR, name)
    with open(path, "w", encoding="utf-8", newline="\n") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
        fp.write("\n")
    return path


def make_descriptions() -> None:
    base = {
        "canvas_width": CANVAS_W,
        "canvas_height": CANVAS_H,
        "background": BACKGROUND,
        "layers": [
            {"name": "底层-红", "file": "layer_bottom.png", "x": 10, "y": 10, "opacity": 1.0},
            {"name": "中层-半透明绿", "file": "layer_middle.png", "x": 60, "y": 40, "opacity": 0.5},
            {"name": "顶层-半透明蓝", "file": "layer_top.png", "x": 130, "y": 100, "opacity": 0.75},
        ],
    }
    _write_json("case1_valid.json", base)

    # 缺一个图层文件：其余层照常合成。
    missing = json.loads(json.dumps(base))
    missing["layers"][1]["file"] = "not_exist_layer.png"
    _write_json("case2_missing_file.json", missing)

    # 不透明度越界（1.5）与缺失：对应层跳过。
    bad_opacity = json.loads(json.dumps(base))
    bad_opacity["layers"][0]["opacity"] = 1.5
    del bad_opacity["layers"][2]["opacity"]
    _write_json("case3_bad_opacity.json", bad_opacity)

    # 小尺寸图层放角落 + 负坐标越界图层：裁剪不报错，画布外保留底色。
    placement = {
        "canvas_width": CANVAS_W,
        "canvas_height": CANVAS_H,
        "background": BACKGROUND,
        "layers": [
            {"name": "角落小图层", "file": "layer_corner.png", "x": 0, "y": 0, "opacity": 1.0},
            {"name": "负坐标图层", "file": "layer_top.png", "x": -40, "y": -20, "opacity": 0.8},
            {"name": "完全越界图层", "file": "layer_middle.png", "x": 999, "y": 999, "opacity": 1.0},
        ],
    }
    _write_json("case4_placement.json", placement)

    # 无法解码的 PNG：跳过该层。
    broken = json.loads(json.dumps(base))
    broken["layers"][2]["file"] = "broken.png"
    _write_json("case5_broken_png.json", broken)

    # 非法底色（5 位、含非法字符各一）：整份描述不可用，无成品。
    for name, bg in (("case6_bad_background.json", "#2b3a5"),
                     ("case7_bad_background2.json", "#zzzzzz")):
        payload = json.loads(json.dumps(base))
        payload["background"] = bg
        _write_json(name, payload)

    # 8 位底色（带 alpha）：合法，可正常合成。
    bg8 = json.loads(json.dumps(base))
    bg8["background"] = "#101820c0"
    _write_json("case8_background_8digit.json", bg8)

    # 全部图层缺失：成品为纯底色，记录标明无可用图层。
    all_missing = {
        "canvas_width": CANVAS_W,
        "canvas_height": CANVAS_H,
        "background": BACKGROUND,
        "layers": [
            {"name": "缺失层A", "file": "ghost_a.png", "x": 0, "y": 0, "opacity": 1.0},
            {"name": "缺失层B", "file": "ghost_b.png", "x": 10, "y": 10, "opacity": 0.5},
        ],
    }
    _write_json("case9_all_missing.json", all_missing)

    # 损坏 JSON：程序正常结束、提示描述不可用、无成品。
    with open(os.path.join(INPUT_DIR, "case10_corrupted.json"), "w",
              encoding="utf-8", newline="\n") as fp:
        fp.write('{"canvas_width": 240, "canvas_height": 180, "layers": [ ,,, ]')


def main() -> None:
    os.makedirs(INPUT_DIR, exist_ok=True)
    make_bottom()
    make_middle()
    make_top()
    make_corner()
    make_broken()
    make_descriptions()
    print(f"样例已生成于：{INPUT_DIR}")
    for name in sorted(os.listdir(INPUT_DIR)):
        print(" -", name)


if __name__ == "__main__":
    main()
