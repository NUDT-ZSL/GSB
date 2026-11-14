# -*- coding: utf-8 -*-
"""生成业务样例：三张植被精灵图（草、花、树叶，均带透明背景）与摆放清单。

确定性绘制（无随机数），重复运行产物一致。

用法:
    python make_sample_assets.py [样例目录]   # 默认 ./sample
"""
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw


def draw_grass(path: Path, size=(240, 180)):
    """草丛：底部簇生的绿色草叶，其余区域透明。"""
    w, h = size
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    blades = [
        (20, 0.35, 0.75), (50, 0.15, 0.95), (80, 0.45, 0.65), (110, 0.05, 1.00),
        (140, 0.55, 0.70), (170, 0.25, 0.90), (200, 0.65, 0.60), (225, 0.35, 0.80),
    ]
    for bx, bend, height_ratio in blades:
        top_x = bx + int((bend - 0.4) * 60)
        top_y = h - int(h * height_ratio)
        color = (34 + bx % 30, 139 + bx % 40, 34 + bx % 20, 255)
        draw.polygon(
            [(bx - 7, h), (bx + 7, h), (top_x + 2, top_y), (top_x - 2, top_y)],
            fill=color,
        )
    img.save(path)


def draw_flower(path: Path, size=(200, 200)):
    """花：旋转花瓣 + 花心 + 花茎，其余区域透明。"""
    w, h = size
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    cx, cy = w // 2, h // 2 - 20
    petal = Image.new("RGBA", (40, 70), (0, 0, 0, 0))
    ImageDraw.Draw(petal).ellipse((2, 2, 38, 68), fill=(218, 112, 160, 255))
    for i in range(6):
        rotated = petal.rotate(i * 60, expand=True, resample=Image.BICUBIC)
        angle = math.radians(i * 60 - 90)
        px = cx + int(math.cos(angle) * 34) - rotated.width // 2
        py = cy + int(math.sin(angle) * 34) - rotated.height // 2
        img.alpha_composite(rotated, (px, py))
    draw = ImageDraw.Draw(img)
    draw.ellipse((cx - 18, cy - 18, cx + 18, cy + 18), fill=(255, 200, 40, 255))
    draw.line((cx, cy + 18, cx, h - 6), fill=(60, 130, 60, 255), width=8)
    img.save(path)


def draw_leaves(path: Path, size=(280, 160)):
    """树叶：几片交错椭圆叶，其余区域透明。"""
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    leaf = Image.new("RGBA", (110, 60), (0, 0, 0, 0))
    ImageDraw.Draw(leaf).ellipse((2, 2, 108, 58), fill=(46, 120, 50, 255))
    placements = [(10, 60, -20), (80, 20, 15), (150, 70, -35), (90, 90, 40), (180, 10, 10)]
    for px, py, angle in placements:
        rotated = leaf.rotate(angle, expand=True, resample=Image.BICUBIC)
        img.alpha_composite(rotated, (px, py))
    img.save(path)


def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "sample"
    assets = root / "assets"
    assets.mkdir(parents=True, exist_ok=True)

    draw_grass(assets / "grass.png")
    draw_flower(assets / "flower.png")
    draw_leaves(assets / "leaves.png")

    import json
    manifest = {
        "canvas": {"width": 800, "height": 600},
        "output_dir": "output",
        "sprites": [
            {
                "name": "grass_patch",
                "source": "assets/grass.png",
                "rect": {"x": 60, "y": 380, "width": 240, "height": 180},
                "shadow": {"offset_x": 10, "offset_y": 12, "blur_radius": 8},
            },
            {
                "name": "flower_bed",
                "source": "assets/flower.png",
                "rect": {"x": 380, "y": 320, "width": 200, "height": 200},
                "shadow": {"offset_x": 8, "offset_y": 10, "blur_radius": 10},
            },
            {
                "name": "leaves_cluster",
                "source": "assets/leaves.png",
                "rect": {"x": 620, "y": 60, "width": 280, "height": 160},
                "shadow": {"offset_x": 12, "offset_y": 8, "blur_radius": 6},
            },
        ],
    }
    with open(root / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"样例已生成: {root}")


if __name__ == "__main__":
    main()
