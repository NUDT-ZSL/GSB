#!/usr/bin/env python3
"""离线游戏浮空文字标签渲染工具。

读取标签清单 JSON 与场景背景 PNG，按清单逐条在指定坐标渲染文字
（含全局描边、阴影配置），合成输出 PNG，并生成逐标签核对报告
（UTF-8 JSON 数组，按清单原顺序）。

用法:
    python render_labels.py --manifest 清单.json --background 背景.png --outdir 输出目录

退出码:
    0  成功（个别标签跳过/不可见不视为失败）
    2  输入错误（清单解析失败、背景损坏、背景尺寸与清单声明不符等）
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFont

# 报告状态
STATUS_RENDERED = "rendered"      # 完整渲染在背景内
STATUS_CLIPPED = "clipped"        # 部分被背景边缘裁剪
STATUS_INVISIBLE = "invisible"    # 完全落在背景外，不可见
STATUS_SKIPPED = "skipped"        # 按规则跳过（空文字 / 字号 < 1）

# 原因码（非跳过/异常时为 null）
REASON_EMPTY_TEXT = "EMPTY_TEXT"
REASON_FONT_TOO_SMALL = "FONT_SIZE_LT_1"
REASON_OUT_OF_BOUNDS = "FULLY_OUT_OF_BOUNDS"

# 候选字体（清单未指定 font_path 时按顺序回退）
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑（中文）
    r"C:\Windows\Fonts\simhei.ttf",    # 黑体
    r"C:\Windows\Fonts\arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def round_half_up(value: float) -> int:
    """四舍五入取整（0.5 向上），用于小数坐标落点。"""
    return int(math.floor(float(value) + 0.5))


def parse_color(value, default):
    """解析 '#RRGGBB' / '#RRGGBBAA' 或 [r,g,b(,a)] 颜色。"""
    if value is None:
        return default
    if isinstance(value, (list, tuple)):
        return tuple(int(c) for c in value)
    s = str(value).lstrip("#")
    if len(s) == 6:
        s += "FF"
    if len(s) != 8:
        raise ValueError(f"无法解析颜色: {value!r}")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4, 6))


def resolve_font_path(style: dict) -> str | None:
    path = style.get("font_path")
    if path:
        if not os.path.isfile(path):
            raise ValueError(f"清单指定的字体文件不存在: {path}")
        return path
    for cand in FONT_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    return None  # 回退 Pillow 内置点阵字体


def load_font(font_path: str | None, size: int):
    if font_path is None:
        return ImageFont.load_default()
    return ImageFont.truetype(font_path, size)


def fail(message: str) -> "SystemExit":
    print(f"[错误] {message}", file=sys.stderr)
    return SystemExit(2)


def load_manifest(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise fail(f"清单文件不存在: {path}")
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise fail(f"清单 JSON 解析失败: {path}: {e}")
    if not isinstance(data, dict) or not isinstance(data.get("labels"), list):
        raise fail("清单格式非法: 顶层需为对象且包含 labels 数组")
    bg = data.get("background") or {}
    if not isinstance(bg.get("width"), int) or not isinstance(bg.get("height"), int):
        raise fail("清单缺少 background.width / background.height 整数声明")
    return data


def load_background(path: str, declared_w: int, declared_h: int) -> Image.Image:
    if not os.path.isfile(path):
        raise fail(f"背景文件不存在: {path}")
    try:
        img = Image.open(path)
        img.load()  # 强制解码，截断/损坏的 PNG 在此抛异常
    except Exception as e:
        raise fail(f"背景 PNG 损坏或无法解码: {path}: {e}")
    if img.size != (declared_w, declared_h):
        raise fail(
            f"背景尺寸 {img.size[0]}x{img.size[1]} 与清单声明 "
            f"{declared_w}x{declared_h} 不符"
        )
    return img.convert("RGBA")


def measure_bbox(draw: ImageDraw.ImageDraw, x: int, y: int, text: str,
                 font, outline_w: int, shadow_dx: int, shadow_dy: int):
    """文字墨迹包围盒（含描边），并并入阴影偏移后的包围盒。"""
    tb = draw.textbbox((x, y), text, font=font, stroke_width=outline_w)
    sb = (tb[0] + shadow_dx, tb[1] + shadow_dy, tb[2] + shadow_dx, tb[3] + shadow_dy)
    return (min(tb[0], sb[0]), min(tb[1], sb[1]),
            max(tb[2], sb[2]), max(tb[3], sb[3]))


def render_labels(bg: Image.Image, manifest: dict, font_path: str | None):
    """逐条渲染，返回 (合成图, 报告列表)。"""
    style = manifest.get("style") or {}
    outline = style.get("outline") or {}
    shadow = style.get("shadow") or {}

    outline_w = max(0, int(outline.get("width", 0)))
    outline_color = parse_color(outline.get("color"), (0, 0, 0, 255))
    shadow_color = parse_color(shadow.get("color"), (0, 0, 0, 160))
    sdx, sdy = (shadow.get("offset") or [0, 0])[:2]
    sdx, sdy = int(sdx), int(sdy)
    fill_color = parse_color(style.get("fill"), (255, 255, 255, 255))

    bg_w, bg_h = bg.size
    sa = manifest.get("safe_area") or {}
    safe_l = int(sa.get("left", 0))
    safe_t = int(sa.get("top", 0))
    safe_r = int(sa.get("right", bg_w))
    safe_b = int(sa.get("bottom", bg_h))

    canvas = bg.copy()
    probe = ImageDraw.Draw(canvas)  # 仅用于测量
    report = []

    for idx, label in enumerate(manifest["labels"]):
        lid = label.get("id", f"#{idx}")
        text = label.get("text")
        font_size = label.get("font_size", 16)

        # 坐标：四舍五入取整
        try:
            x = round_half_up(label.get("x", 0))
            y = round_half_up(label.get("y", 0))
        except (TypeError, ValueError):
            report.append({
                "id": lid, "status": STATUS_SKIPPED, "position": None,
                "overflow_px": 0, "safe_area_overflow": False,
                "reason_code": "INVALID_COORD",
            })
            continue

        # 跳过规则：空文字 / 字号 < 1
        if text is None or str(text) == "":
            report.append({
                "id": lid, "status": STATUS_SKIPPED, "position": [x, y],
                "overflow_px": 0, "safe_area_overflow": False,
                "reason_code": REASON_EMPTY_TEXT,
            })
            continue
        text = str(text)
        try:
            fs = float(font_size)
        except (TypeError, ValueError):
            fs = 0.0
        if fs < 1:
            report.append({
                "id": lid, "status": STATUS_SKIPPED, "position": [x, y],
                "overflow_px": 0, "safe_area_overflow": False,
                "reason_code": REASON_FONT_TOO_SMALL,
            })
            continue

        font = load_font(font_path, max(1, int(round(fs))))
        bbox = measure_bbox(probe, x, y, text, font, outline_w, sdx, sdy)

        # 可见性判定
        intersects = bbox[2] > 0 and bbox[0] < bg_w and bbox[3] > 0 and bbox[1] < bg_h
        fully_inside = (bbox[0] >= 0 and bbox[1] >= 0
                        and bbox[2] <= bg_w and bbox[3] <= bg_h)

        # 安全区溢出像素（取四边超出量的最大值）
        overflow = max(
            0,
            bbox[2] - safe_r, safe_l - bbox[0],
            bbox[3] - safe_b, safe_t - bbox[1],
        )

        if not intersects:
            status, reason = STATUS_INVISIBLE, REASON_OUT_OF_BOUNDS
        else:
            # 实际绘制：阴影 + 描边文字，画在透明层上再合成，
            # 越界部分由画布边界自然裁剪，不中断整批渲染。
            layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
            ld = ImageDraw.Draw(layer)
            if sdx or sdy:
                ld.text((x + sdx, y + sdy), text, font=font, fill=shadow_color,
                        stroke_width=outline_w, stroke_fill=shadow_color)
            ld.text((x, y), text, font=font, fill=fill_color,
                    stroke_width=outline_w, stroke_fill=outline_color)
            canvas = Image.alpha_composite(canvas, layer)
            status = STATUS_RENDERED if fully_inside else STATUS_CLIPPED
            reason = None

        report.append({
            "id": lid,
            "status": status,
            "position": [x, y],
            "overflow_px": int(overflow),
            "safe_area_overflow": overflow > 0,
            "reason_code": reason,
        })

    return canvas.convert("RGB"), report


def atomic_write_bytes(path: str, data: bytes):
    """先写临时文件再替换，避免留下残缺输出。"""
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="离线浮空文字标签渲染工具")
    ap.add_argument("--manifest", required=True, help="标签清单 JSON 路径")
    ap.add_argument("--background", required=True, help="场景背景 PNG 路径")
    ap.add_argument("--outdir", required=True, help="输出目录")
    ap.add_argument("--image-name", default="composited.png", help="输出图片文件名")
    ap.add_argument("--report-name", default="report.json", help="报告文件名")
    args = ap.parse_args(argv)

    manifest = load_manifest(args.manifest)
    declared = manifest["background"]
    bg = load_background(args.background, declared["width"], declared["height"])

    try:
        font_path = resolve_font_path(manifest.get("style") or {})
    except ValueError as e:
        return fail(str(e))

    image, report = render_labels(bg, manifest, font_path)

    os.makedirs(args.outdir, exist_ok=True)
    image_path = os.path.join(args.outdir, args.image_name)
    report_path = os.path.join(args.outdir, args.report_name)

    import io
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    atomic_write_bytes(image_path, buf.getvalue())
    atomic_write_bytes(
        report_path,
        json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"),
    )

    counts = {}
    for item in report:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    print(f"渲染完成: {image_path}")
    print(f"核对报告: {report_path} (共 {len(report)} 条: {summary})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
