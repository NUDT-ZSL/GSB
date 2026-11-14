# -*- coding: utf-8 -*-
"""
确定性测试样本生成器。

运行后在 samples/ 下生成:
  test_2048x1024.png   主验收纹理 (棋盘格 + 坐标刻度 + 固定随机图形, 完全确定性)
  test_256x128.png     小于视口的小纹理 (用于验证居中与边界)
  broken.png           损坏 PNG (合法文件头 + 垃圾字节)
  text_only.png        纯文本伪装成 PNG
  config_valid.json    合法配置
  config_empty.json    空 JSON ({})
  config_bad.json      全越界/类型错误配置

用法: python make_samples.py
"""

import json
import os
import random

from PIL import Image, ImageDraw

SAMPLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")
SEED = 83


def _draw_texture(path, width, height):
    """生成内容确定性的 PNG: 128px 棋盘格 + 边框 + 坐标刻度 + 种子图形。"""
    img = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    # 128px 棋盘格 (与默认网格间距一致, 便于肉眼核对网格对齐)
    cell = 128
    for row in range(math_ceil(height / cell)):
        for col in range(math_ceil(width / cell)):
            if (row + col) % 2 == 0:
                x0, y0 = col * cell, row * cell
                draw.rectangle([x0, y0, min(x0 + cell, width), min(y0 + cell, height)],
                               fill=(228, 232, 238))

    # 固定随机图形 (种子固定 => 每次生成字节一致)
    rng = random.Random(SEED)
    palette = [(220, 60, 60), (60, 130, 220), (60, 180, 110),
               (210, 150, 40), (150, 80, 200)]
    for _ in range(40):
        x0 = rng.randint(0, width - 1)
        y0 = rng.randint(0, height - 1)
        w = rng.randint(20, 160)
        h = rng.randint(20, 160)
        color = palette[rng.randrange(len(palette))]
        if rng.random() < 0.5:
            draw.rectangle([x0, y0, min(x0 + w, width - 1), min(y0 + h, height - 1)],
                           outline=color, width=3)
        else:
            draw.ellipse([x0, y0, min(x0 + w, width - 1), min(y0 + h, height - 1)],
                         outline=color, width=3)

    # 每 128px 画刻度, 每 256px 标注纹理坐标
    for x in range(0, width + 1, cell):
        draw.line([(x, 0), (x, min(10, height))], fill=(0, 0, 0), width=2)
        if x % 256 == 0 and x < width:
            draw.text((x + 3, 12), str(x), fill=(0, 0, 0))
    for y in range(0, height + 1, cell):
        draw.line([(0, y), (min(10, width), y)], fill=(0, 0, 0), width=2)
        if y % 256 == 0 and y < height:
            draw.text((12, y + 3), str(y), fill=(0, 0, 0))

    # 四角 L 形定位标记 (不同颜色, 便于检查平移衔接)
    corner = 40
    corner_colors = [(255, 0, 0), (0, 200, 0), (0, 0, 255), (255, 140, 0)]
    corners = [
        (0, 0, 1, 1),
        (width - 1, 0, -1, 1),
        (0, height - 1, 1, -1),
        (width - 1, height - 1, -1, -1),
    ]
    for (cx, cy, sx, sy), color in zip(corners, corner_colors):
        draw.line([(cx, cy), (cx + sx * corner, cy)], fill=color, width=4)
        draw.line([(cx, cy), (cx, cy + sy * corner)], fill=color, width=4)

    # 红色外边框
    draw.rectangle([0, 0, width - 1, height - 1], outline=(200, 0, 0), width=2)
    img.save(path, format="PNG")


def math_ceil(value):
    # 独立小工具, 避免文件顶部 import 顺序干扰阅读
    import math
    return math.ceil(value)


def main():
    os.makedirs(SAMPLE_DIR, exist_ok=True)

    tex_path = os.path.join(SAMPLE_DIR, "test_2048x1024.png")
    small_path = os.path.join(SAMPLE_DIR, "test_256x128.png")
    _draw_texture(tex_path, 2048, 1024)
    _draw_texture(small_path, 256, 128)

    # 损坏 PNG: 合法签名后接确定性垃圾字节
    broken = os.path.join(SAMPLE_DIR, "broken.png")
    rng = random.Random(SEED)
    garbage = bytes(rng.randrange(256) for _ in range(512))
    with open(broken, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + garbage)

    # 纯文本伪装
    with open(os.path.join(SAMPLE_DIR, "text_only.png"), "wb") as fh:
        fh.write(b"this is not a png file, just plain text\n" * 8)

    with open(os.path.join(SAMPLE_DIR, "config_valid.json"), "w", encoding="utf-8") as fh:
        json.dump({"window_width": 1024, "window_height": 768,
                   "initial_scale": 2.0, "grid_spacing": 64}, fh, indent=2)
    with open(os.path.join(SAMPLE_DIR, "config_empty.json"), "w", encoding="utf-8") as fh:
        json.dump({}, fh)
    with open(os.path.join(SAMPLE_DIR, "config_bad.json"), "w", encoding="utf-8") as fh:
        json.dump({"window_width": -5, "window_height": 999999,
                   "initial_scale": 99.0, "grid_spacing": 0,
                   "window_size": "nope", "extra": [1, 2]}, fh, indent=2)

    print("样本已生成于 %s" % SAMPLE_DIR)
    for name in sorted(os.listdir(SAMPLE_DIR)):
        size = os.path.getsize(os.path.join(SAMPLE_DIR, name))
        print("  %-22s %8d bytes" % (name, size))


if __name__ == "__main__":
    main()
