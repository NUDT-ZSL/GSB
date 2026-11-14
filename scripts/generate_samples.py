"""确定性生成验收所需的精灵表、补丁图与配置（无随机源）。

生成内容（samples/ 下）：
- base_96x96.png    4x4 网格（24px 格，可整除），每格固定可识别配色
- base_100x100.png  4x4 网格（25px 格，可整除，用于宽度 100 的边带验收）
- base_100x96.png   4x4 网格（25x24，右侧/底部各余 0/0；另含不可整除案例配置）
- base_98x94.png    4x4 网格（24x23，右侧余 2、底部余 2 的边带案例）
- patch_*.png       各类补丁（正常、过大、过小、浮点偏移、透明角、半透明、损坏、缺失）
- *.json            对应配置
重复运行生成字节完全一致的资产（固定色值，PNG 无时间戳依赖 Pillow 默认）。
"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"


def cell_fill(r: int, c: int, x: int, y: int, w: int, h: int) -> tuple[int, int, int, int]:
    """每格固定配色：基色按行列确定，内部带 4px 边框与对角渐变，便于肉眼核对。"""
    base_r = 60 + r * 45
    base_g = 90 + c * 40
    base_b = 140 + ((r * 7 + c * 13) % 8) * 12
    border = 4
    if x < border or y < border or x >= w - border or y >= h - border:
        return (255, 255, 255, 255)
    shade = (x + y) % 24
    return (base_r + shade, base_g + shade, base_b, 255)


def make_sheet(width: int, height: int, rows: int, cols: int,
               cell_w: int, cell_h: int, path: Path) -> None:
    img = Image.new("RGBA", (width, height), (20, 20, 24, 255))
    px = img.load()
    for r in range(rows):
        for c in range(cols):
            left, top = c * cell_w, r * cell_h
            for y in range(top, top + cell_h):
                for x in range(left, left + cell_w):
                    px[x, y] = cell_fill(r, c, x - left, y - top, cell_w, cell_h)
    img.save(path, format="PNG")


def solid_patch(path: Path, w: int, h: int, color: tuple[int, int, int],
                transparent_corners: bool = False) -> None:
    img = Image.new("RGBA", (w, h), color + (255,))
    if transparent_corners:
        px = img.load()
        for y in range(h):
            for x in range(w):
                if (x < 3 and y < 3) or (x >= w - 3 and y >= h - 3):
                    px[x, y] = (0, 0, 0, 0)  # 完全透明角，验证“透明不覆盖”
    img.save(path, format="PNG")


def main() -> None:
    SAMPLES.mkdir(exist_ok=True)

    # ---- 底图 ----
    make_sheet(96, 96, 4, 4, 24, 24, SAMPLES / "base_96x96.png")
    make_sheet(100, 100, 4, 4, 25, 25, SAMPLES / "base_100x100.png")
    make_sheet(98, 94, 4, 4, 24, 23, SAMPLES / "base_98x94.png")

    # ---- 补丁 ----
    solid_patch(SAMPLES / "patch_cell0_red.png", 24, 24, (239, 68, 68), True)
    solid_patch(SAMPLES / "patch_cell0_green.png", 24, 24, (34, 197, 94), True)
    solid_patch(SAMPLES / "patch_oversize.png", 40, 18, (168, 85, 247))
    solid_patch(SAMPLES / "patch_small.png", 12, 10, (14, 165, 233))
    solid_patch(SAMPLES / "patch_float.png", 10, 10, (245, 158, 11))
    solid_patch(SAMPLES / "patch_semi.png", 24, 24, (255, 255, 255), False)
    semi = Image.open(SAMPLES / "patch_semi.png").convert("RGBA")
    sp = semi.load()
    for y in range(semi.height):
        for x in range(semi.width):
            sp[x, y] = (255, 255, 255, 128)  # 半透明，验证 alpha 混合而非直接替换
    semi.save(SAMPLES / "patch_semi.png", format="PNG")
    solid_patch(SAMPLES / "patch_corrupt.png", 8, 8, (0, 0, 0))
    (SAMPLES / "patch_corrupt.png").write_bytes(b"NOT-A-PNG-\x00\x01\x02broken")
    # patch_missing.png 故意不落盘，供“文件不存在”分支使用

    def write_json(name: str, sheet: str, w: int, h: int,
                   rows: int, cols: int, patches: list) -> None:
        (SAMPLES / name).write_text(
            json.dumps(
                {
                    "sheet": {"width": w, "height": h},
                    "grid": {"rows": rows, "columns": cols},
                    "patches": patches,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    # 主综合案例：覆盖顺序叠加/裁剪/小补丁/浮点/损坏/缺失/半透明
    write_json(
        "config_main.json", "base_96x96.png", 96, 96, 4, 4,
        [
            {"row": 0, "column": 0, "path": "patch_cell0_red.png"},
            {"row": 0, "column": 0, "path": "patch_cell0_green.png"},
            {"index": 5, "path": "patch_oversize.png",
             "offset_x": 14, "offset_y": 6},
            {"row": 2, "column": 1, "path": "patch_small.png",
             "offset_x": 3, "offset_y": 5},
            {"row": 3, "column": 3, "path": "patch_float.png",
             "offset_x": 2.6, "offset_y": -1.4},
            {"index": 10, "path": "patch_semi.png"},
            {"row": 1, "column": 2, "path": "patch_corrupt.png"},
            {"index": 14, "path": "patch_missing.png"},
        ],
    )
    # 空清单：无实质修改
    write_json("config_empty.json", "base_96x96.png", 96, 96, 4, 4, [])
    # 宽 100 / 4 列：可整除，右侧余数 0
    write_json(
        "config_band100.json", "base_100x100.png", 100, 100, 4, 4,
        [{"index": 0, "path": "patch_small.png", "offset_x": 1, "offset_y": 1}],
    )
    # 不可整除：右侧余 2、底部余 2
    write_json(
        "config_band98.json", "base_98x94.png", 98, 94, 4, 4,
        [{"row": 0, "column": 0, "path": "patch_cell0_red.png"}],
    )
    # 缺 grid.rows / grid.columns
    (SAMPLES / "config_missing_grid.json").write_text(
        json.dumps(
            {
                "sheet": {"width": 96, "height": 96},
                "grid": {},
                "patches": [{"index": 0, "path": "patch_small.png"}],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    # 非法字段：index 越界 + offset 类型错误 + 未知字段
    write_json(
        "config_bad_fields.json", "base_96x96.png", 96, 96, 4, 4,
        [
            {"index": 99, "path": "patch_small.png", "offset_x": "abc"},
            {"row": 0, "column": 0, "path": "patch_small.png", "zoom": 2},
        ],
    )

    print(f"样本已生成于 {SAMPLES}")


if __name__ == "__main__":
    main()
