# -*- coding: utf-8 -*-
"""确定性生成示例素材：各部位灰度遮罩 PNG（L/LA/RGB）、打包配置 JSON 与损坏样本。

不依赖任何外部素材，也不使用随机数，重复运行产出逐字节一致的图片。
"""
from __future__ import annotations

import json
import os
import sys

from PIL import Image


def _pattern_value(x: int, y: int, offset: int) -> int:
    return (x * 3 + y * 5 + offset) % 256


def _make_gray(size, offset: int) -> Image.Image:
    w, h = size
    img = Image.new("L", size)
    img.putdata([_pattern_value(x, y, offset) for y in range(h) for x in range(w)])
    return img


def _make_la(size, offset: int) -> Image.Image:
    w, h = size
    l = _make_gray(size, offset)
    alpha = Image.new("L", size)
    alpha.putdata([(255 - _pattern_value(x, y, offset)) for y in range(h) for x in range(w)])
    return Image.merge("LA", (l, alpha))


def _make_rgb(size, offset: int) -> Image.Image:
    """RGB 三通道取相同值，画面与同参数灰度图一致。"""
    l = _make_gray(size, offset)
    return Image.merge("RGB", (l, l, l))


def _write_truncated_png(path: str) -> None:
    """先写完整 PNG，再截断一半字节，制造损坏/截断样本。"""
    tmp = _make_gray((64, 64), 99)
    tmp_path = path + ".full.png"
    tmp.save(tmp_path, format="PNG")
    with open(tmp_path, "rb") as f:
        data = f.read()
    os.remove(tmp_path)
    with open(path, "wb") as f:
        f.write(data[: len(data) // 2])


# 基础素材定义：(文件名, 模式, 尺寸, 图案偏移)
_SOURCE_DEFS = [
    ("helmet_metal.png", "L", (64, 64), 10),
    ("helmet_rough.png", "L", (64, 64), 20),
    ("helmet_ao.png", "RGB", (64, 64), 30),
    ("helmet_opacity.png", "L", (64, 64), 40),
    ("chest_metal.png", "LA", (64, 64), 50),
    ("arm_rough.png", "L", (80, 40), 60),   # 宽于单元，验证越界裁剪
    ("leg_ao.png", "RGB", (64, 64), 70),
    ("boot_opacity.png", "L", (64, 64), 80),
    ("bad_anchor_part.png", "L", (64, 64), 90),  # 配置中给越界锚点
]


def _part_entry(name: str, channel: str, cell, anchor) -> dict:
    stem = os.path.splitext(name)[0]
    return {
        "id": stem,
        "source": f"sources/{name}",
        "cell": list(cell),
        "anchor": list(anchor),
        "channel": channel,
    }


def generate(workspace: str) -> str:
    """在 workspace 下生成 sources/、output/ 与 pack_config.json，返回配置路径。"""
    src_dir = os.path.join(workspace, "sources")
    out_dir = os.path.join(workspace, "output")
    os.makedirs(src_dir, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)

    for name, mode, size, offset in _SOURCE_DEFS:
        if mode == "L":
            img = _make_gray(size, offset)
        elif mode == "LA":
            img = _make_la(size, offset)
        else:
            img = _make_rgb(size, offset)
        img.save(os.path.join(src_dir, name), format="PNG")

    # 与 helmet_metal 同画面的 RGB 版本（三通道同值），用于灰度/RGB 一致性核对
    _make_rgb((64, 64), 10).save(os.path.join(src_dir, "helmet_metal_rgb.png"), format="PNG")

    # 截断 PNG 损坏样本
    _write_truncated_png(os.path.join(src_dir, "broken_part.png"))

    parts = [
        _part_entry("helmet_metal.png", "R", (0, 0), (0, 0)),
        _part_entry("helmet_rough.png", "G", (1, 0), (0, 0)),
        _part_entry("helmet_ao.png", "B", (2, 0), (0, 0)),
        _part_entry("helmet_opacity.png", "A", (3, 0), (0, 0)),
        _part_entry("chest_metal.png", "R", (0, 1), (0, 0)),
        _part_entry("arm_rough.png", "G", (1, 1), (8, 0)),
        _part_entry("leg_ao.png", "B", (2, 1), (0, 0)),
        _part_entry("boot_opacity.png", "A", (3, 1), (0, 0)),
        _part_entry("broken_part.png", "R", (0, 2), (0, 0)),       # 损坏 -> 跳过
        _part_entry("bad_anchor_part.png", "G", (1, 2), (100, 100)),  # 锚点越界 -> 跳过
    ]

    config = {
        "atlas": {"width": 256, "height": 256},
        "grid": {"cols": 4, "rows": 4},
        "output": {
            "atlas_path": "output/atlas.png",
            "report_path": "output/report.json",
        },
        "preview": {
            "x": 20,
            "y": 10,
            "width": 180,
            "height": 140,
            "output_path": "output/preview.png",
        },
        "parts": parts,
    }
    config_path = os.path.join(workspace, "pack_config.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    return config_path


def main(argv: list[str]) -> int:
    workspace = argv[1] if len(argv) > 1 else os.path.join(os.getcwd(), "sample_workspace")
    os.makedirs(workspace, exist_ok=True)
    path = generate(workspace)
    print(f"示例素材与配置已生成: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
