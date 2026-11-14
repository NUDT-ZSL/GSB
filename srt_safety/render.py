"""代表帧 PNG 渲染：深色画幅、黄色安全区框、同屏字幕错位排布。"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .audit import CueRecord
from .config import CanvasConfig

_MEASURE_IMG = Image.new("RGB", (4, 4))
_MEASURE_DRAW = ImageDraw.Draw(_MEASURE_IMG)

BG_COLOR = (18, 18, 24)
SAFE_COLOR = (255, 214, 64)       # 安全区明黄
TARGET_COLOR = (255, 255, 255)    # 当前句白色
PEER_COLOR = (170, 190, 210)      # 同屏其它字幕灰蓝
INVERTED_COLOR = (255, 96, 96)    # 倒置红色
FRAME_COLOR = (120, 255, 170)     # 当前句包围框
WARN_COLOR = (255, 140, 0)        # 越界橙色


def render_frame(cfg: CanvasConfig, font, target: CueRecord,
                 visible: list[CueRecord], out_path: str | Path) -> None:
    img = Image.new("RGB", (cfg.width, cfg.height), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # 安全区矩形
    draw.rectangle(
        (cfg.safe_x, cfg.safe_y,
         cfg.width - cfg.safe_x - 1, cfg.height - cfg.safe_y - 1),
        outline=SAFE_COLOR, width=2,
    )

    # 头部信息（小字位图字体，不参与排版度量）
    header_font = ImageFont.load_default()
    header = (
        f"#{target.index}  frame {target.start_frame}-{target.end_frame}"
        f"   on-screen cues: {len(visible)}   safe area (yellow)"
    )
    draw.text((cfg.safe_x, max(2, cfg.safe_y - 14)), header,
              fill=SAFE_COLOR, font=header_font)
    if target.outside_safe:
        direction_map = {"左": "L", "右": "R", "上": "T", "下": "B"}
        sides = "/".join(direction_map.get(d, "?") for d in target.overflow_directions)
        warn = f"! OUT OF SAFE AREA: {sides}"
        warn_w = _MEASURE_DRAW.textlength(warn, font=header_font)
        draw.text((cfg.width - cfg.safe_x - warn_w - 2, max(2, cfg.safe_y - 14)),
                  warn, fill=WARN_COLOR, font=header_font)

    center_x = (target.rect["x0"] + target.rect["x1"]) // 2

    for rec in visible:
        if rec.inverted:
            fill = INVERTED_COLOR
        elif rec.index == target.index:
            fill = TARGET_COLOR
        else:
            fill = PEER_COLOR

        rec_center = (rec.rect["x0"] + rec.rect["x1"]) // 2
        top = rec.rect["y0"]
        for line in rec.lines:
            line_w = line["width_px"]
            draw.text((rec_center - line_w // 2, top), line["text"],
                      fill=fill, font=font)
            top += cfg.line_height

        if rec.index == target.index:
            if rec.inverted:
                frame_color = INVERTED_COLOR
            elif rec.outside_safe:
                frame_color = WARN_COLOR
            else:
                frame_color = FRAME_COLOR
            # 钳制在画布内：越界样本的包围框不被画布边缘裁掉
            box = (
                max(0, rec.rect["x0"] - 4),
                max(0, rec.rect["y0"] - 2),
                min(cfg.width - 1, rec.rect["x1"] + 4),
                min(cfg.height - 1, rec.rect["y1"] + 2),
            )
            draw.rectangle(box, outline=frame_color, width=1)
            # 标记标签置于块内左上角，避免同屏多句时压到相邻泳道
            if rec.inverted:
                tag = " INVERTED "
                tag_color = INVERTED_COLOR
            elif rec.outside_safe:
                tag = " OUT OF SAFE "
                tag_color = WARN_COLOR
            else:
                tag = f" CUE #{rec.index} "
                tag_color = FRAME_COLOR
            tag_x = min(rec.rect["x0"], cfg.width - 110)
            tag_y = max(0, rec.rect["y0"] - 12)
            draw.text((max(0, tag_x), tag_y), tag, fill=tag_color, font=header_font)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG")
