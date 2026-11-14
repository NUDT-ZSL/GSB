"""联系表 PNG 排版：固定背景/网格线色、配置驱动的网格布局与文字标注。

确定性保证：
  - 字体按固定优先级解析，最终回落到 Pillow 内置位图字体（不依赖系统字体枚举）；
  - 不写入任何时间戳类 PNG 块，Pillow 固定压缩级别，同输入必然同字节。
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFont

from .config import SheetConfig
from .errors import ConfigError

# 字体解析的固定优先级，跨机器保持一致；含 CJK 候选以正确渲染中文标注
_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑（Windows 中文）
    r"C:\Windows\Fonts\simhei.ttf",    # 黑体
    r"C:\Windows\Fonts\arial.ttf",     # Arial（纯英文环境）
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/Library/Fonts/Arial.ttf",
]
HEADER_HEIGHT = 34


def _load_font(font_size: int):
    for candidate in _FONT_CANDIDATES:
        if os.path.isfile(candidate):
            try:
                return ImageFont.truetype(candidate, font_size)
            except OSError:
                continue
    return ImageFont.load_default()


def _load_header_font(font_size: int):
    return _load_font(max(font_size, 14))


def build_sheet(records: list[dict], config: SheetConfig,
                video_path: str, note: str | None) -> Image.Image:
    """根据定位记录构建联系表位图（尚未落盘）。"""
    # 联系表单元格：已定位且未被合并掉的镜头（含异常清单条目，照常出图但保留原因）
    cells = [r for r in records if r["status"] == "located" and not r["merged"]]

    cols = config.columns
    rows = max(1, (len(cells) + cols - 1) // cols)
    label_height = config.font_size + 8
    cell_w = config.thumb_width
    cell_h = config.thumb_height + label_height
    grid_line_width = 1

    width = config.padding * 2 + cols * cell_w + (cols - 1) * config.spacing
    height = (config.padding + HEADER_HEIGHT
              + rows * cell_h + (rows - 1) * config.spacing
              + config.padding)

    bg = Image.new("RGB", (width, height), config.background_color)
    draw = ImageDraw.Draw(bg)
    font = _load_font(config.font_size)
    header_font = _load_header_font(config.font_size)

    header = f"联系表  |  视频: {os.path.basename(video_path)}  |  代表帧: {len(cells)}"
    draw.text((config.padding, config.padding), header,
              fill=config.header_color, font=header_font)

    if not cells:
        empty_text = note or "无镜头：清单为空或没有可定位的帧"
        draw.text((config.padding, config.padding + HEADER_HEIGHT),
                  empty_text, fill=config.text_color, font=font)
        return bg

    top0 = config.padding + HEADER_HEIGHT
    for index, rec in enumerate(cells):
        row_idx, col_idx = divmod(index, cols)
        x0 = config.padding + col_idx * (cell_w + config.spacing)
        y0 = top0 + row_idx * (cell_h + config.spacing)

        thumb: Image.Image = rec["image"]
        bg.paste(thumb, (x0, y0))
        # 网格线：缩略图外框 1px，颜色可由配置覆盖
        draw.rectangle(
            (x0 - grid_line_width, y0 - grid_line_width,
             x0 + cell_w, y0 + config.thumb_height),
            outline=config.grid_color, width=grid_line_width)

        label = config.label_template.format(frame=rec["frame"], ms=rec["ms"])
        draw.text((x0, y0 + config.thumb_height + 4),
                  label, fill=config.text_color, font=font)
    return bg


def save_png(image: Image.Image, out_path: str) -> None:
    """以固定参数写入 PNG，保证重复运行字节一致。"""
    try:
        image.save(out_path, format="PNG", optimize=False, compress_level=6)
    except OSError as exc:
        raise ConfigError(f"联系表 PNG 写入失败：{out_path}（{exc}）") from exc
