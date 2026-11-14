"""确定性生成业务样例：samples/atlas.png 与 samples/animations.json。

不使用任何外部附件，固定随机种子，重复运行产物完全一致。
图集 128x64，帧网格 32x32（4 列 x 2 行，共 8 格），内容为一个
横版像素小人的行走/眨眼动画帧，含半透明阴影以覆盖 alpha 场景。
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from PIL import Image, ImageDraw

FW = FH = 32
COLS, ROWS = 4, 2
OUT_DIR = Path(__file__).resolve().parent / "samples"

BODY = (64, 120, 216, 255)
SKIN = (240, 200, 160, 255)
PANTS = (40, 44, 52, 255)
SHADOW = (0, 0, 0, 90)  # 半透明地面阴影


def draw_character(img: Image.Image, cell: int, leg_offset: int,
                   eye_open: bool) -> None:
    # 种子只取决于姿态参数：相同姿态必然画出相同像素，保证
    # idle/blink 中的"无变化帧"逐像素一致，结果可确定性重建。
    rng = random.Random(f"pose:{leg_offset}:{eye_open}")
    ox, oy = (cell % COLS) * FW, (cell // COLS) * FH
    d = ImageDraw.Draw(img)
    # 半透明椭圆阴影
    d.ellipse([ox + 8, oy + 27, ox + 24, oy + 31], fill=SHADOW)
    # 腿（行走时前后错开 leg_offset 像素）
    d.rectangle([ox + 12 + leg_offset, oy + 20, ox + 14 + leg_offset, oy + 28],
                fill=PANTS)
    d.rectangle([ox + 17 - leg_offset, oy + 20, ox + 19 - leg_offset, oy + 28],
                fill=PANTS)
    # 身体与头
    d.rectangle([ox + 10, oy + 10, ox + 21, oy + 21], fill=BODY)
    d.rectangle([ox + 12, oy + 3, ox + 19, oy + 10], fill=SKIN)
    # 眼睛：睁开为两个黑点，闭上为一条线
    if eye_open:
        d.point([(ox + 14, oy + 6), (ox + 17, oy + 6)], fill=(0, 0, 0, 255))
    else:
        d.line([ox + 14, oy + 6, ox + 17, oy + 6], fill=(0, 0, 0, 255))
    # 确定性噪点装饰（固定种子），模拟衣料纹理
    for _ in range(6):
        x = ox + rng.randrange(10, 22)
        y = oy + rng.randrange(11, 21)
        d.point((x, y), fill=(BODY[0] - 20, BODY[1] - 20, BODY[2] - 20, 255))


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    atlas = Image.new("RGBA", (COLS * FW, ROWS * FH), (0, 0, 0, 0))

    # 行走循环 0-3：经典四帧步态，末帧->首帧存在正常步进差异，
    # 阈值 0 下会被标记为循环跳变（演示工具抓出衔接问题）
    draw_character(atlas, 0, leg_offset=0, eye_open=True)
    draw_character(atlas, 1, leg_offset=2, eye_open=True)
    draw_character(atlas, 2, leg_offset=0, eye_open=True)
    draw_character(atlas, 3, leg_offset=-2, eye_open=True)
    # 眨眼 4-6：4 与 5 完全相同，6 为闭眼帧，与 4 形成循环跳变
    draw_character(atlas, 4, leg_offset=0, eye_open=True)
    draw_character(atlas, 5, leg_offset=0, eye_open=True)
    draw_character(atlas, 6, leg_offset=0, eye_open=False)
    # 待机 7：与 4 完全相同（演示"无变化帧"与平滑循环）
    draw_character(atlas, 7, leg_offset=0, eye_open=True)

    atlas.save(OUT_DIR / "atlas.png", format="PNG")

    desc = {
        "comment": "横版像素游戏动画描述样例：帧序号 = 行优先网格序号",
        "animations": [
            {"name": "walk", "frame_width": FW, "frame_height": FH,
             "frames": [0, 1, 2, 3], "diff_threshold": 0, "loop": True},
            {"name": "blink", "frame_width": FW, "frame_height": FH,
             "frames": [4, 5, 6], "diff_threshold": 0, "loop": True},
            {"name": "idle", "frame_width": FW, "frame_height": FH,
             "frames": [4, 7], "diff_threshold": 0, "loop": True},
        ],
    }
    (OUT_DIR / "animations.json").write_text(
        json.dumps(desc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"samples written to {OUT_DIR}")


if __name__ == "__main__":
    main()
