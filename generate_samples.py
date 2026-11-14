"""确定性生成样例小图与图集配置, 用于本地复现验证。

用法:
    python generate_samples.py [样例根目录]   # 默认 ./sample_data

生成内容(全部固定参数, 无随机因素):
    sprites/red.png        32x32 纯红不透明
    sprites/green.png      24x40 绿图, 顶部亮绿标记(非正方形, 验证旋转方向)
    sprites/blue.png       16x16 纯蓝不透明
    sprites/fade.png       20x20 黄色, 四边半透明渐变(验证 alpha 保留)
    sprites/big.png        96x96 紫色不透明(小画布配置下溢出)
    config.json            128x128 画布, bleed=4, 含一张旋转小图
    config_small.json      48x48 小画布, big.png 溢出
    config_missing.json    含指向不存在文件的小图
    config_invalid.json    缺少 canvas 字段的非法配置
"""

from __future__ import annotations

import json
import os
import sys

from PIL import Image


def solid(w: int, h: int, rgba: tuple[int, int, int, int]) -> Image.Image:
    return Image.new("RGBA", (w, h), rgba)


def green_marker(w: int = 24, h: int = 40) -> Image.Image:
    """非正方形绿图: 顶部 8 行亮绿, 其余暗绿, 用于验证旋转方向。"""
    img = Image.new("RGBA", (w, h), (0, 128, 0, 255))
    for y in range(8):
        for x in range(w):
            img.putpixel((x, y), (0, 255, 0, 255))
    return img


def fade_edges(size: int) -> Image.Image:
    """黄色小图, 距边缘越近 alpha 越低(确定性的线性渐变)。"""
    img = Image.new("RGBA", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            d = min(x, y, size - 1 - x, size - 1 - y)
            alpha = min(255, d * 64)  # 边缘 alpha=0, 向内每像素 +64
            px[x, y] = (255, 255, 0, alpha)
    return img


def main() -> None:
    root = sys.argv[1] if len(sys.argv) > 1 else "sample_data"
    sprites_dir = os.path.join(root, "sprites")
    os.makedirs(sprites_dir, exist_ok=True)

    solid(32, 32, (255, 0, 0, 255)).save(os.path.join(sprites_dir, "red.png"))
    green_marker().save(os.path.join(sprites_dir, "green.png"))
    solid(16, 16, (0, 0, 255, 255)).save(os.path.join(sprites_dir, "blue.png"))
    fade_edges(20).save(os.path.join(sprites_dir, "fade.png"))
    solid(96, 96, (128, 0, 128, 255)).save(os.path.join(sprites_dir, "big.png"))

    def write_config(name: str, cfg: dict) -> None:
        with open(os.path.join(root, name), "w", encoding="utf-8", newline="\n") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.write("\n")

    write_config("config.json", {
        "canvas": {"width": 128, "height": 128},
        "bleed": 4,
        "sprites": [
            {"name": "red", "src": "sprites/red.png"},
            {"name": "green", "src": "sprites/green.png", "rotate": True},
            {"name": "blue", "src": "sprites/blue.png"},
            {"name": "fade", "src": "sprites/fade.png"},
        ],
    })
    write_config("config_small.json", {
        "canvas": {"width": 48, "height": 48},
        "bleed": 4,
        "sprites": [
            {"name": "red", "src": "sprites/red.png"},
            {"name": "big", "src": "sprites/big.png"},
        ],
    })
    write_config("config_missing.json", {
        "canvas": {"width": 128, "height": 128},
        "bleed": 4,
        "sprites": [
            {"name": "red", "src": "sprites/red.png"},
            {"name": "ghost", "src": "sprites/does_not_exist.png"},
            {"name": "blue", "src": "sprites/blue.png"},
        ],
    })
    write_config("config_invalid.json", {
        "bleed": 4,
        "sprites": [{"name": "red", "src": "sprites/red.png"}],
    })

    print(f"样例已生成: {os.path.abspath(root)}")


if __name__ == "__main__":
    main()
