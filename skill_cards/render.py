"""PNG 检视卡渲染（需求 6、12）。

每张卡尺寸严格等于清单画布尺寸，自上而下：
标题栏 → 镜头缩放带 → 中心点轨迹 → 音效时间轴 → 音效明细表 → 图例。
越界音效统一画在右侧越界区并在明细表中标红。
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFont

from .model import FIT_CLOSE, FIT_OFF, FIT_TIGHT, FIT_LABELS, Sound
from .pipeline import CompletedSkill, format_num

# ---------------------------------------------------------------- 配色
COLOR_BG = (255, 255, 255)
COLOR_INK = (38, 42, 51)
COLOR_GRAY = (120, 126, 138)
COLOR_PANEL_BG = (247, 248, 250)
COLOR_PANEL_BORDER = (206, 210, 218)
COLOR_ZOOM = (37, 99, 235)       # 缩放带：蓝
COLOR_CX = (13, 148, 136)        # 中心 x：青
COLOR_CY = (124, 58, 237)        # 中心 y：紫
COLOR_KF = (155, 160, 170)       # 关键帧虚线
COLOR_TIGHT = (22, 163, 74)      # 精准：绿
COLOR_CLOSE = (217, 119, 6)      # 贴近：橙
COLOR_OFF = (200, 35, 51)        # 偏离：红
COLOR_OOB_BG = (254, 235, 235)
COLOR_OOB = (220, 38, 38)        # 越界：红
COLOR_GRID = (228, 230, 235)

FIT_COLORS = {FIT_TIGHT: COLOR_TIGHT, FIT_CLOSE: COLOR_CLOSE, FIT_OFF: COLOR_OFF}

_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\Deng.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/PingFang.ttc",
]


def load_fonts(size_body: int) -> tuple[ImageFont.FreeTypeFont | ImageFont.ImageFont, ...]:
    """返回 (标题, 正文, 小字) 字体，优先系统中文字体，失败回退默认位图字体。"""
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                body = ImageFont.truetype(path, size_body)
                title = ImageFont.truetype(path, size_body + 5)
                small = ImageFont.truetype(path, max(size_body - 3, 9))
                return title, body, small
            except OSError:
                continue
    default = ImageFont.load_default()
    return default, default, default


def _dashed_vline(draw: ImageDraw.ImageDraw, x: float, y0: float, y1: float,
                  color: tuple[int, int, int], dash: int = 4, gap: int = 3) -> None:
    y = y0
    while y < y1:
        draw.line([(x, y), (x, min(y + dash, y1))], fill=color, width=1)
        y += dash + gap


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> float:
    try:
        return draw.textlength(text, font=font)
    except AttributeError:  # pragma: no cover - 极旧 Pillow 回退
        return font.getsize(text)[0]


def _nice_range(values: list[float], min_span: float) -> tuple[float, float]:
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        lo -= min_span / 2
        hi += min_span / 2
    pad = (hi - lo) * 0.12
    return lo - pad, hi + pad


def _y_map(lo: float, hi: float, top: float, bottom: float):
    span = hi - lo if hi > lo else 1.0

    def map_y(v: float) -> float:
        return bottom - (v - lo) / span * (bottom - top)

    return map_y


def _triangle(draw: ImageDraw.ImageDraw, x: float, y: float, size: float,
              color: tuple[int, int, int], point_down: bool = True) -> None:
    if point_down:
        pts = [(x, y + size), (x - size * 0.85, y - size * 0.6),
               (x + size * 0.85, y - size * 0.6)]
    else:
        pts = [(x, y - size), (x - size * 0.85, y + size * 0.6),
               (x + size * 0.85, y + size * 0.6)]
    draw.polygon(pts, fill=color)


def render_card(skill: CompletedSkill, out_path: str) -> None:
    w, h = skill.canvas_w, skill.canvas_h
    img = Image.new("RGB", (w, h), COLOR_BG)
    draw = ImageDraw.Draw(img)

    base = max(11, min(20, round(min(w, h) / 42)))
    font_title, font_body, font_small = load_fonts(base)

    m = max(12, round(min(w, h) * 0.022))
    gap = max(6, round(base * 0.6))

    # ---------------- 布局：右侧预留越界区 ----------------
    oob_w = max(44, round(w * 0.075))
    plot_left = m + max(46, round(w * 0.075))
    plot_right = w - m - oob_w - 8
    plot_w = plot_right - plot_left
    strip_l, strip_r = plot_right + 5, w - m

    title_h = round(base * 2.6)
    head_h = round(base * 1.9)           # 面板内标题行
    zoom_h = max(104, round(h * 0.17))
    center_h = zoom_h
    timeline_h = max(86, round(h * 0.13))
    row_h = round(base * 1.75)
    table_h = (len(skill.sounds) + 1) * row_h + m
    legend_h = round(base * 2.4)

    # 画布偏小时等比压缩面板，保证不溢出
    used = (m + title_h + gap + zoom_h + gap + center_h + gap
            + timeline_h + gap + table_h + gap + legend_h + m)
    if used > h:
        overflow = used - h
        shrink = min(overflow, round((zoom_h + center_h) * 0.3))
        zoom_h -= shrink // 2
        center_h -= shrink // 2

    y = m
    # ---------------- 标题栏 ----------------
    oob_part = f"，越界 {skill.out_of_bounds_count}" if skill.out_of_bounds_count else ""
    title = (f"{skill.page_name}    总时长 {format_num(skill.duration_ms, 1)} ms"
             f"    画布 {w}×{h}    音效 {skill.total_sounds}{oob_part}")
    draw.text((m, y), title, fill=COLOR_INK, font=font_title)
    y += title_h

    duration = skill.duration_ms

    def x_of(t: float) -> float:
        return plot_left + (t / duration) * plot_w

    keyframes = skill.keyframes
    in_bounds = [s for s in skill.sounds if not s.out_of_bounds]
    oob_sounds = [s for s in skill.sounds if s.out_of_bounds]

    def draw_panel(top: float, panel_h: float, panel_title: str) -> tuple[float, float]:
        box = [plot_left - 6, top, plot_right, top + panel_h]
        draw.rectangle(box, fill=COLOR_PANEL_BG, outline=COLOR_PANEL_BORDER)
        draw.rectangle([strip_l, top, strip_r, top + panel_h],
                       fill=COLOR_OOB_BG, outline=COLOR_OOB)
        # 标题独占面板内首行，不与绘图区/刻度重叠
        draw.text((plot_left, top + 2), panel_title, fill=COLOR_INK, font=font_small)
        draw.line([(plot_left - 6, top + head_h), (plot_right, top + head_h)],
                  fill=COLOR_PANEL_BORDER)
        return top + head_h + base * 0.35, top + panel_h - base * 0.55

    # ---------------- 镜头缩放带 ----------------
    z_top, z_bottom = draw_panel(y, zoom_h, "镜头缩放带")
    z_lo, z_hi = _nice_range([kf.zoom for kf in keyframes], 0.2)
    z_map = _y_map(z_lo, z_hi, z_top, z_bottom)
    # 水平网格 + y 轴刻度
    for i in range(3):
        gv = z_lo + (z_hi - z_lo) * i / 2
        gy = z_map(gv)
        draw.line([(plot_left, gy), (plot_right, gy)], fill=COLOR_GRID)
        draw.text((m, gy - base * 0.55), format_num(gv, 2),
                  fill=COLOR_GRAY, font=font_small)
    # 关键帧虚线
    for kf in keyframes:
        _dashed_vline(draw, x_of(kf.time_ms), z_top, z_bottom, COLOR_KF)
    # 缩放折线（采样以表现线性段）
    pts: list[tuple[float, float]] = []
    samples = max(2, round(plot_w / 6))
    for i in range(samples + 1):
        t = duration * i / samples
        kf0, kf1 = keyframes[0], keyframes[-1]
        if t <= kf0.time_ms:
            zv = kf0.zoom
        elif t >= kf1.time_ms:
            zv = kf1.zoom
        else:
            for a, b in zip(keyframes, keyframes[1:]):
                if a.time_ms <= t <= b.time_ms:
                    r = (t - a.time_ms) / (b.time_ms - a.time_ms) \
                        if b.time_ms != a.time_ms else 0.0
                    zv = a.zoom + (b.zoom - a.zoom) * r
                    break
            else:
                zv = kf1.zoom
        pts.append((x_of(t), z_map(zv)))
    draw.line(pts, fill=COLOR_ZOOM, width=2)
    for kf in keyframes:
        draw.ellipse([x_of(kf.time_ms) - 3, z_map(kf.zoom) - 3,
                      x_of(kf.time_ms) + 3, z_map(kf.zoom) + 3],
                     fill=COLOR_ZOOM, outline=COLOR_BG)
    y += zoom_h + gap

    # ---------------- 中心点轨迹 ----------------
    c_top, c_bottom = draw_panel(y, center_h, "中心点轨迹（x 青 / y 紫）")
    c_lo, c_hi = _nice_range(
        [v for kf in keyframes for v in (kf.cx, kf.cy)], 1.0
    )
    c_map = _y_map(c_lo, c_hi, c_top, c_bottom)
    for i in range(3):
        gv = c_lo + (c_hi - c_lo) * i / 2
        gy = c_map(gv)
        draw.line([(plot_left, gy), (plot_right, gy)], fill=COLOR_GRID)
        draw.text((m, gy - base * 0.55), format_num(gv, 1),
                  fill=COLOR_GRAY, font=font_small)
    for kf in keyframes:
        _dashed_vline(draw, x_of(kf.time_ms), c_top, c_bottom, COLOR_KF)

    def center_poly(axis: str, color: tuple[int, int, int]) -> None:
        line_pts = []
        for i in range(samples + 1):
            t = duration * i / samples
            kf0, kf1 = keyframes[0], keyframes[-1]
            if t <= kf0.time_ms:
                v = kf0.cx if axis == "x" else kf0.cy
            elif t >= kf1.time_ms:
                v = kf1.cx if axis == "x" else kf1.cy
            else:
                for a, b in zip(keyframes, keyframes[1:]):
                    if a.time_ms <= t <= b.time_ms:
                        r = (t - a.time_ms) / (b.time_ms - a.time_ms) \
                            if b.time_ms != a.time_ms else 0.0
                        av, bv = (a.cx, b.cx) if axis == "x" else (a.cy, b.cy)
                        v = av + (bv - av) * r
                        break
                else:
                    v = kf1.cx if axis == "x" else kf1.cy
            line_pts.append((x_of(t), c_map(v)))
        draw.line(line_pts, fill=color, width=2)

    center_poly("x", COLOR_CX)
    center_poly("y", COLOR_CY)
    y += center_h + gap

    # ---------------- 音效时间轴 ----------------
    tl_top = y
    tl_bottom = y + timeline_h
    draw.rectangle([plot_left - 6, tl_top, plot_right, tl_bottom],
                   fill=COLOR_PANEL_BG, outline=COLOR_PANEL_BORDER)
    draw.rectangle([strip_l, tl_top, strip_r, tl_bottom],
                   fill=COLOR_OOB_BG, outline=COLOR_OOB)
    # 面板标题独占第一行；关键帧标签放第二行，碰撞时在同行内左右避让
    title_text = "音效落点时间轴"
    draw.text((plot_left, tl_top + 1), title_text, fill=COLOR_INK, font=font_small)
    draw.line([(plot_left - 6, tl_top + head_h), (plot_right, tl_top + head_h)],
              fill=COLOR_PANEL_BORDER)
    draw.text((strip_l + 3, tl_top + 1), "越界区", fill=COLOR_OOB, font=font_small)

    kf_label_y = tl_top + round(base * 1.02)
    placed: list[tuple[float, float]] = []
    for kf in keyframes:
        kx = x_of(kf.time_ms)
        klabel = f"KF {format_num(kf.time_ms, 1)}"
        kw = _text_width(draw, klabel, font_small)
        lx = kx - kw / 2
        for ox, ow in placed:  # 简单推挤：与已放标签重叠就右移
            if lx < ox + ow + 6 and lx + kw + 6 > ox:
                lx = ox + ow + 6
        lx = min(lx, plot_right - kw)
        draw.text((lx, kf_label_y), klabel, fill=COLOR_GRAY, font=font_small)
        placed.append((lx, kw))

    axis_y = tl_top + head_h + round((tl_bottom - tl_top - head_h) * 0.34)
    draw.line([(plot_left, axis_y), (plot_right, axis_y)], fill=COLOR_GRAY, width=2)
    # 时间刻度：短刻度在轴上，文字在轴下方第一行
    for i in range(5):
        t = duration * i / 4
        tx = x_of(t)
        draw.line([(tx, axis_y - 3), (tx, axis_y + 3)], fill=COLOR_GRAY)
        label = format_num(t, 1)
        draw.text((tx - _text_width(draw, label, font_small) / 2,
                   axis_y + base * 0.7), label, fill=COLOR_GRAY, font=font_small)
    # 关键帧菱形贴在轴上方
    for kf in keyframes:
        kx = x_of(kf.time_ms)
        draw.polygon([(kx, axis_y - 11), (kx - 4, axis_y - 7),
                      (kx, axis_y - 3), (kx + 4, axis_y - 7)], fill=COLOR_KF)
    y += timeline_h + gap

    # ---------------- 越界音效：跨面板红色虚线 + 标记 ----------------
    body_top = m + title_h + gap
    body_bottom = tl_bottom
    if oob_sounds:
        step = (strip_r - strip_l) / len(oob_sounds)
        for i, snd in enumerate(oob_sounds):
            sx = strip_l + step * (i + 0.5)
            _dashed_vline(draw, sx, body_top, body_bottom, COLOR_OOB, dash=3, gap=3)
            _triangle(draw, sx, body_top + 6, base * 0.55, COLOR_OOB)
            tlabel = format_num(snd.t, 1)
            draw.text((sx - _text_width(draw, tlabel, font_small) / 2,
                       body_top + base * 1.1), tlabel,
                      fill=COLOR_OOB, font=font_small)

    # 合法音效：三个面板上沿的落点三角；时间轴标签放轴下方、刻度文字再下一行
    for snd in in_bounds:
        sx = x_of(snd.t)
        color = FIT_COLORS[snd.fit] if snd.fit else COLOR_GRAY
        _triangle(draw, sx, z_top + 4, base * 0.5, color)
        _triangle(draw, sx, c_top + 4, base * 0.5, color)
        _triangle(draw, sx, axis_y - 5, base * 0.55, color, point_down=False)
        tlabel = format_num(snd.t, 1)
        tw = _text_width(draw, tlabel, font_small)
        # 音效数字放在轴刻度文字之下，避免与时间刻度重叠
        draw.text((sx - tw / 2, axis_y + base * 1.75), tlabel,
                  fill=color, font=font_small)

    # ---------------- 音效明细表 ----------------
    cols = [("音效", 0.20), ("时刻ms", 0.12), ("缩放", 0.11),
            ("中心点(x,y)", 0.21), ("间隔ms", 0.13), ("贴合/越界", 0.23)]
    table_w = plot_right - (plot_left - 6)
    x_cols = [plot_left - 6]
    for _, frac in cols:
        x_cols.append(x_cols[-1] + table_w * frac)

    draw.rectangle([x_cols[0], y, x_cols[-1], y + row_h],
                   fill=(238, 241, 246), outline=COLOR_PANEL_BORDER)
    for ci, (cname, _) in enumerate(cols):
        draw.text((x_cols[ci] + 4, y + row_h * 0.18), cname,
                  fill=COLOR_INK, font=font_small)
    y += row_h

    for snd in skill.sounds:
        if snd.out_of_bounds:
            values = [
                snd.label,
                format_num(snd.t, 1),
                "—", "—", "—",
                f"越界（须在 0~{format_num(duration, 1)}ms 内）",
            ]
            row_color = COLOR_OOB
        else:
            values = [
                snd.label,
                format_num(snd.t, 1),
                format_num(snd.zoom, 3),
                f"({format_num(snd.cx, 2)}, {format_num(snd.cy, 2)})",
                format_num(snd.gap_ms, 1),
                f"{FIT_LABELS[snd.fit]}（最近帧 {format_num(snd.nearest_kf_ms, 1)}ms）",
            ]
            row_color = FIT_COLORS[snd.fit] if snd.fit else COLOR_INK
        for ci, val in enumerate(values):
            draw.text((x_cols[ci] + 4, y + row_h * 0.18), str(val),
                      fill=row_color if ci == 5 else COLOR_INK, font=font_small)
        draw.line([(x_cols[0], y + row_h), (x_cols[-1], y + row_h)],
                  fill=COLOR_PANEL_BORDER)
        y += row_h
    for x in x_cols:
        draw.line([(x, y - (len(skill.sounds) + 1) * row_h), (x, y)],
                  fill=COLOR_PANEL_BORDER)
    y += gap

    # ---------------- 图例 ----------------
    legend_items = [
        (COLOR_TIGHT, f"{FIT_LABELS[FIT_TIGHT]} 间隔≤50ms"),
        (COLOR_CLOSE, f"{FIT_LABELS[FIT_CLOSE]} 50<间隔≤150ms"),
        (COLOR_OFF, f"{FIT_LABELS[FIT_OFF]} 间隔>150ms"),
        (COLOR_OOB, "越界（不参与贴合度）"),
        (COLOR_KF, "镜头关键帧"),
    ]
    lx = m
    for color, text in legend_items:
        draw.rectangle([lx, y + 2, lx + base, y + base + 2],
                       fill=color, outline=COLOR_PANEL_BORDER)
        draw.text((lx + base + 4, y), text, fill=COLOR_INK, font=font_small)
        lx += base + 8 + _text_width(draw, text, font_small) + base * 1.4

    img.save(out_path, format="PNG")
