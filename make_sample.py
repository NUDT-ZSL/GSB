#!/usr/bin/env python3
"""生成可复现的业务样例：含透明区域的 PNG 图集 + JSON 区域配置（含各类异常项）。

运行：python make_sample.py
产物：
  sample_data/atlas.png            带 alpha 的 64x48 图集
  sample_data/regions.json         覆盖成功/全透明/无效/越界/重名等场景的配置
  sample_data/broken.png           损坏文件（不是合法 PNG）
  sample_data/rgb_no_alpha.png     RGB 无 alpha 的 PNG
  sample_data/broken_config.json   无法解析的 JSON
  sample_data/object_config.json   JSON 对象（非数组）
"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

DATA_DIR = Path(__file__).parent / "sample_data"


def build_atlas() -> Image.Image:
    """64x48 RGBA 图集，内含不透明、半透明与全透明精灵。"""
    img = Image.new("RGBA", (64, 48), (0, 0, 0, 0))
    px = img.load()

    # hero：(2,2) 16x16，红色不透明主体，右边两列半透明（模拟溢色/半透明瑕疵观察对象）。
    for y in range(2, 18):
        for x in range(2, 18):
            if x < 14:
                px[x, y] = (220, 40, 40, 255)
            else:
                px[x, y] = (220, 40, 40, 110)  # 半透明边缘

    # edge：(40,4) 20x20，蓝色半透明块。
    for y in range(4, 24):
        for x in range(40, 60):
            px[x, y] = (40, 80, 220, 160)

    # dup 第一份：(2,24) 10x10 绿色不透明块。
    for y in range(24, 34):
        for x in range(2, 12):
            px[x, y] = (40, 180, 60, 255)

    # dup 第二份（同名，后出现，应覆盖前者）：(16,24) 10x10 黄色不透明块。
    for y in range(24, 34):
        for x in range(16, 26):
            px[x, y] = (230, 200, 40, 255)

    # pad_test：(44,30) 12x12 紫色小块。
    for y in range(30, 42):
        for x in range(44, 56):
            px[x, y] = (160, 60, 200, 255)

    # ghost 区域 (22,2) 12x12 刻意不绘制任何像素 -> alpha 全 0（全透明区域）。
    return img


def build_regions() -> list[dict]:
    """按配置原始顺序排列，覆盖全部状态分支。"""
    return [
        # 1. 正常区域
        {"name": "hero", "x": 2, "y": 2, "width": 16, "height": 16},
        # 2. 有效区域但裁剪结果全透明
        {"name": "ghost", "x": 22, "y": 2, "width": 12, "height": 12},
        # 3. 缺字段（缺 width）-> config_invalid
        {"name": "missing_width", "x": 30, "y": 2, "height": 8},
        # 4. 字符串坐标 -> config_invalid
        {"name": "string_coord", "x": "2", "y": 2, "width": 8, "height": 8},
        # 5. 负坐标 -> invalid_coordinates（不裁剪修正）
        {"name": "negative", "x": -4, "y": 2, "width": 8, "height": 8},
        # 6. 越界（x+width=70 > 64）-> out_of_bounds（不裁剪修正）
        {"name": "out_of_bounds", "x": 50, "y": 2, "width": 20, "height": 8},
        # 7. 同名区域第一份 -> superseded
        {"name": "dup", "x": 2, "y": 24, "width": 10, "height": 10},
        # 8. 同名区域第二份 -> 成功，内容为黄色块
        {"name": "dup", "x": 16, "y": 24, "width": 10, "height": 10},
        # 9. 半透明正常区域
        {"name": "edge", "x": 40, "y": 4, "width": 20, "height": 20},
        # 10. 正常小区域
        {"name": "pad_test", "x": 44, "y": 30, "width": 12, "height": 12},
    ]


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    atlas = build_atlas()
    atlas.save(DATA_DIR / "atlas.png", format="PNG")

    (DATA_DIR / "regions.json").write_text(
        json.dumps(build_regions(), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 损坏文件：合法 PNG 头部 + 被截断的随机字节。
    broken = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) + b"\x00" * 32
    (DATA_DIR / "broken.png").write_bytes(broken)

    # RGB 无 alpha 图集。
    Image.new("RGB", (32, 32), (10, 20, 30)).save(
        DATA_DIR / "rgb_no_alpha.png", format="PNG"
    )

    # 无法解析的 JSON。
    (DATA_DIR / "broken_config.json").write_text(
        '[{"name": "oops", "x": 1, "y": 2,', encoding="utf-8"
    )

    # 合法 JSON 但顶层是对象而非数组。
    (DATA_DIR / "object_config.json").write_text(
        json.dumps({"name": "not_an_array", "x": 0, "y": 0, "width": 4, "height": 4}),
        encoding="utf-8",
    )

    print(f"样例数据已生成于：{DATA_DIR}")
    for p in sorted(DATA_DIR.iterdir()):
        print(f"  {p.name}  ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
