#!/usr/bin/env python3
"""生成示例图层清单与 PNG 图片（确定性重建：固定内容，无随机）。"""
import json
import os

from PIL import Image, ImageDraw

SAMPLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample")


def make_images(d):
    # 1. 底色层 800x600：浅蓝底 + 网格
    bg = Image.new("RGBA", (800, 600), (200, 220, 240, 255))
    dr = ImageDraw.Draw(bg)
    for x in range(0, 800, 50):
        dr.line([(x, 0), (x, 600)], fill=(180, 200, 225, 255))
    for y in range(0, 600, 50):
        dr.line([(0, y), (800, y)], fill=(180, 200, 225, 255))
    bg.save(os.path.join(d, "bg.png"))

    # 2. multiply 层 300x300：红色圆
    mul = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
    ImageDraw.Draw(mul).ellipse([20, 20, 280, 280], fill=(255, 120, 120, 255))
    mul.save(os.path.join(d, "multiply_circle.png"))

    # 3. screen 层 300x300：绿色圆
    scr = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
    ImageDraw.Draw(scr).ellipse([20, 20, 280, 280], fill=(120, 255, 120, 200))
    scr.save(os.path.join(d, "screen_circle.png"))

    # 4. overlay 模式层（模式不受支持，应被跳过）
    ov = Image.new("RGBA", (100, 100), (255, 200, 0, 255))
    ov.save(os.path.join(d, "overlay_mode.png"))

    # 5. opacity 越界层（应被跳过）
    bo = Image.new("RGBA", (100, 100), (0, 0, 255, 255))
    bo.save(os.path.join(d, "bad_opacity.png"))

    # 6. 负偏移层 200x200，offsetX=-50（仅右侧 150 列可见）
    off = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    ImageDraw.Draw(off).rectangle([0, 0, 199, 199], fill=(255, 0, 255, 255))
    off.save(os.path.join(d, "offset_layer.png"))

    # 7. 尺寸不符层：清单声明 100x100，实际 50x50
    mm = Image.new("RGBA", (50, 50), (0, 255, 0, 255))
    mm.save(os.path.join(d, "mismatch.png"))

    # 8. 全透明层（应计为成功，且画面不变）
    tr = Image.new("RGBA", (400, 300), (0, 0, 0, 0))
    tr.save(os.path.join(d, "transparent.png"))

    # 9. 正常装饰层：黄色矩形，半透明
    deco = Image.new("RGBA", (150, 150), (0, 0, 0, 0))
    ImageDraw.Draw(deco).rectangle([10, 10, 140, 140], fill=(255, 230, 0, 255))
    deco.save(os.path.join(d, "deco.png"))

    # missing.png 故意不生成，用于演示“文件缺失”


def make_manifest(d):
    manifest = {
        "width": 800,
        "height": 600,
        "layers": [
            {"file": "bg.png", "mode": "normal", "opacity": 1.0,
             "offsetX": 0, "offsetY": 0, "width": 800, "height": 600},
            {"file": "multiply_circle.png", "mode": "multiply", "opacity": 0.9,
             "offsetX": 100, "offsetY": 80, "width": 300, "height": 300},
            {"file": "screen_circle.png", "mode": "screen", "opacity": 0.8,
             "offsetX": 350, "offsetY": 200, "width": 300, "height": 300},
            {"file": "deco.png", "mode": "normal", "opacity": 0.6,
             "offsetX": 600, "offsetY": 400, "width": 150, "height": 150},
            {"file": "offset_layer.png", "mode": "normal", "opacity": 1.0,
             "offsetX": -50, "offsetY": 350, "width": 200, "height": 200},
            {"file": "transparent.png", "mode": "normal", "opacity": 1.0,
             "offsetX": 0, "offsetY": 0, "width": 400, "height": 300},
            {"file": "overlay_mode.png", "mode": "overlay", "opacity": 1.0,
             "offsetX": 0, "offsetY": 0, "width": 100, "height": 100},
            {"file": "bad_opacity.png", "mode": "normal", "opacity": 1.5,
             "offsetX": 0, "offsetY": 0, "width": 100, "height": 100},
            {"file": "mismatch.png", "mode": "normal", "opacity": 1.0,
             "offsetX": 0, "offsetY": 0, "width": 100, "height": 100},
            {"file": "missing.png", "mode": "normal", "opacity": 1.0,
             "offsetX": 0, "offsetY": 0, "width": 100, "height": 100},
        ],
    }
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


def main():
    os.makedirs(SAMPLE_DIR, exist_ok=True)
    make_images(SAMPLE_DIR)
    make_manifest(SAMPLE_DIR)
    print(f"示例已生成于: {SAMPLE_DIR}")


if __name__ == "__main__":
    main()
