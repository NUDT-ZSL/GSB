# -*- coding: utf-8 -*-
"""
vector_render.py —— 二维矢量画面 JSON 渲染命令行工具（仅使用 Python 标准库）

功能：
  * 解析美术团队提供的场景 JSON（画布宽高、背景色、有序图元列表）
  * 按输出扩展名渲染为 PNG（纯 Python 光栅化 + zlib 编码）或 SVG（文本拼接）
  * 生成与图像同主名的 .txt 文本报告（画布尺寸、成功数、跳过数、逐条跳过原因）
  * 非法图元跳过并记录索引/原因；场景级错误打印指明字段的中文错误并非零退出

用法：
  python vector_render.py input.json output.png
  python vector_render.py input.json output.svg
"""

import argparse
import json
import math
import os
import re
import struct
import sys
import tempfile
import zlib

MAX_DIM = 4096
SUPPORTED_FORMATS = (".png", ".svg")
KNOWN_TYPES = ("rect", "circle", "line")

_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


class SceneError(Exception):
    """场景级致命错误（无法继续渲染），消息中带字段名，供 CLI 打印并以非零码退出。"""


# --------------------------------------------------------------------------- #
# 基础校验
# --------------------------------------------------------------------------- #
def _is_number(value):
    """int/float 且排除 bool（JSON 里 true/false 不是合法数字）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int_number(value):
    return _is_number(value)


def parse_color(value, field):
    """解析 #rgb / #rrggbb 颜色，返回标准化的 #rrggbb 小写字符串。"""
    if not isinstance(value, str):
        raise ValueError("字段 %s 的颜色必须是字符串，实际类型为 %s" % (field, type(value).__name__))
    text = value.strip()
    if not _COLOR_RE.match(text):
        raise ValueError("字段 %s 的颜色格式错误：%r，应为 #rgb 或 #rrggbb" % (field, value))
    text = text.lower()
    if len(text) == 4:  # #rgb -> #rrggbb
        text = "#" + "".join(ch * 2 for ch in text[1:])
    return text


def _parse_opacity(value):
    if not _is_number(value):
        raise ValueError("透明度 opacity 必须是数字，实际类型为 %s" % type(value).__name__)
    if not (0.0 <= float(value) <= 1.0):
        raise ValueError("透明度 opacity 越界：%r，应在 [0, 1] 区间" % (value))
    return float(value)


def _parse_line_width(value):
    if not _is_number(value):
        raise ValueError("线宽 line_width 必须是数字，实际类型为 %s" % type(value).__name__)
    if value < 0:
        raise ValueError("线宽 line_width 不能为负数：%r" % (value))
    return float(value)


# --------------------------------------------------------------------------- #
# 场景解析（场景级问题抛 SceneError；图元级问题收集到 skips）
# --------------------------------------------------------------------------- #
def load_scene(path):
    """读取并解析 JSON 文件；文件不存在 / JSON 非法时抛 SceneError。"""
    if not os.path.isfile(path):
        raise SceneError("输入文件不存在：%s" % path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise SceneError("输入文件 %s 内容不是合法 JSON：%s" % (path, exc.msg)) from exc
    except OSError as exc:
        raise SceneError("无法读取输入文件 %s：%s" % (path, exc)) from exc
    if not isinstance(data, dict):
        raise SceneError("场景描述必须是 JSON 对象，实际类型为 %s" % type(data).__name__)
    return data


def _validate_canvas(data):
    # width
    if "width" not in data:
        raise SceneError("缺少字段：width（画布宽度）")
    width = data["width"]
    if not _is_int_number(width):
        raise SceneError("字段 width 类型非法：应为整数，实际为 %s" % type(width).__name__)
    if width <= 0 or width > MAX_DIM:
        raise SceneError("字段 width 宽度越界：%s，允许范围为 1~%d" % (width, MAX_DIM))

    # height
    if "height" not in data:
        raise SceneError("缺少字段：height（画布高度）")
    height = data["height"]
    if not _is_int_number(height):
        raise SceneError("字段 height 类型非法：应为整数，实际为 %s" % type(height).__name__)
    if height <= 0 or height > MAX_DIM:
        raise SceneError("字段 height 高度越界：%s，允许范围为 1~%d" % (height, MAX_DIM))

    # background
    if "background" not in data:
        raise SceneError("缺少字段：background（背景色）")
    try:
        background = parse_color(data["background"], "background")
    except ValueError as exc:
        raise SceneError(str(exc)) from exc

    # primitives
    if "primitives" not in data:
        raise SceneError("缺少字段：primitives（图元列表）")
    primitives = data["primitives"]
    if not isinstance(primitives, list):
        raise SceneError("字段 primitives 类型非法：应为列表，实际为 %s"
                         % type(primitives).__name__)

    return int(width), int(height), background, primitives


def _normalize_primitive(item, index):
    """
    校验并归一化单个图元。
    成功返回 dict（含归一化后的数值与颜色）；非法时抛 ValueError（原因写入报告）。
    """
    if not isinstance(item, dict):
        raise ValueError("图元必须是 JSON 对象，实际类型为 %s" % type(item).__name__)
    if "type" not in item:
        raise ValueError("缺少 type 字段")

    ptype = item["type"]
    if ptype not in KNOWN_TYPES:
        raise ValueError("未知图元类型：%r（支持的类型：rect/circle/line）" % ptype)

    opacity = _parse_opacity(item["opacity"]) if "opacity" in item else 1.0
    line_width = _parse_line_width(item["line_width"]) if "line_width" in item else None

    if ptype == "rect":
        for field in ("x", "y", "width", "height"):
            if field not in item:
                raise ValueError("rect 图元缺少字段：%s" % field)
            if not _is_number(item[field]):
                raise ValueError("rect 图元字段 %s 必须是数字，实际类型为 %s"
                                 % (field, type(item[field]).__name__))
        if item["width"] <= 0:
            raise ValueError("rect 图元 width 必须为正数：%r" % item["width"])
        if item["height"] <= 0:
            raise ValueError("rect 图元 height 必须为正数：%r" % item["height"])
        if "color" not in item:
            raise ValueError("rect 图元缺少字段：color")
        color = parse_color(item["color"], "color")
        return {
            "type": "rect",
            "x": float(item["x"]), "y": float(item["y"]),
            "width": float(item["width"]), "height": float(item["height"]),
            "color": color, "opacity": opacity,
            "line_width": line_width,  # None 表示纯填充；>0 表示仅描边
        }

    if ptype == "circle":
        for field in ("cx", "cy", "radius"):
            if field not in item:
                raise ValueError("circle 图元缺少字段：%s" % field)
            if not _is_number(item[field]):
                raise ValueError("circle 图元字段 %s 必须是数字，实际类型为 %s"
                                 % (field, type(item[field]).__name__))
        if item["radius"] <= 0:
            raise ValueError("circle 图元 radius 必须为正数（不允许负半径/零半径）：%r"
                             % item["radius"])
        if "color" not in item:
            raise ValueError("circle 图元缺少字段：color")
        color = parse_color(item["color"], "color")
        return {
            "type": "circle",
            "cx": float(item["cx"]), "cy": float(item["cy"]),
            "radius": float(item["radius"]),
            "color": color, "opacity": opacity,
            "line_width": line_width,
        }

    # line
    for field in ("x1", "y1", "x2", "y2"):
        if field not in item:
            raise ValueError("line 图元缺少字段：%s" % field)
        if not _is_number(item[field]):
            raise ValueError("line 图元字段 %s 必须是数字，实际类型为 %s"
                             % (field, type(item[field]).__name__))
    if "color" not in item:
        raise ValueError("line 图元缺少字段：color")
    color = parse_color(item["color"], "color")
    return {
        "type": "line",
        "x1": float(item["x1"]), "y1": float(item["y1"]),
        "x2": float(item["x2"]), "y2": float(item["y2"]),
        "color": color, "opacity": opacity,
        "line_width": line_width if line_width is not None else 1.0,
    }


def prepare_scene(data):
    """场景级校验 + 图元逐条校验，返回 (scene, skips)。"""
    width, height, background, raw_items = _validate_canvas(data)
    scene = {"width": width, "height": height, "background": background, "items": []}
    skips = []
    for index, raw in enumerate(raw_items):
        try:
            scene["items"].append(_normalize_primitive(raw, index))
        except ValueError as exc:
            skips.append({"index": index, "reason": str(exc)})
    return scene, skips


# --------------------------------------------------------------------------- #
# PNG 光栅化（纯 Python，整数 alpha 合成，结果与常规 8-bit 合成一致）
# --------------------------------------------------------------------------- #
def _hex_to_rgb(color):
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


class Rasterizer:
    def __init__(self, width, height, background):
        self.width = width
        self.height = height
        bg = _hex_to_rgb(background)
        self.pixels = bytearray(bg) * (width * height)

    def _blend(self, x, y, rgb, opacity):
        """整数 alpha 合成：out = src*a + dst*(255-a)，再除以 255。"""
        if not (0 <= x < self.width and 0 <= y < self.height) or opacity <= 0.0:
            return
        a = int(round(opacity * 255.0))
        if a <= 0:
            return
        if a >= 255:
            off = (y * self.width + x) * 3
            self.pixels[off:off + 3] = bytes(rgb)
            return
        off = (y * self.width + x) * 3
        sr, sg, sb = rgb
        self.pixels[off] = (sr * a + self.pixels[off] * (255 - a)) // 255
        self.pixels[off + 1] = (sg * a + self.pixels[off + 1] * (255 - a)) // 255
        self.pixels[off + 2] = (sb * a + self.pixels[off + 2] * (255 - a)) // 255

    def _fill_bbox(self, x0, y0, x1, y1, rgb, opacity, inside):
        x0 = max(0, int(math.floor(x0)))
        y0 = max(0, int(math.floor(y0)))
        x1 = min(self.width - 1, int(math.ceil(x1)))
        y1 = min(self.height - 1, int(math.ceil(y1)))
        for py in range(y0, y1 + 1):
            cy = py + 0.5
            for px in range(x0, x1 + 1):
                if inside(px + 0.5, cy):
                    self._blend(px, py, rgb, opacity)

    def draw_rect(self, item):
        rgb = _hex_to_rgb(item["color"])
        x, y = item["x"], item["y"]
        w, h = item["width"], item["height"]
        lw = item["line_width"]
        if lw is None:
            # 填充矩形：像素中心落在 [x, x+w] × [y, y+h] 内
            self._fill_bbox(x, y, x + w, y + h, rgb, item["opacity"],
                            lambda cx, cy: x <= cx <= x + w and y <= cy <= y + h)
        elif lw > 0:
            half = lw / 2.0

            def inside(cx, cy):
                if not (x <= cx <= x + w and y <= cy <= y + h):
                    return False
                return (cx <= x + half or cx >= x + w - half
                        or cy <= y + half or cy >= y + h - half)

            self._fill_bbox(x - half, y - half, x + w + half, y + h + half,
                            rgb, item["opacity"], inside)

    def draw_circle(self, item):
        rgb = _hex_to_rgb(item["color"])
        ccx, ccy, r = item["cx"], item["cy"], item["radius"]
        lw = item["line_width"]
        if lw is None:
            self._fill_bbox(ccx - r, ccy - r, ccx + r, ccy + r, rgb, item["opacity"],
                            lambda cx, cy: (cx - ccx) ** 2 + (cy - ccy) ** 2 <= r * r)
        elif lw > 0:
            half = lw / 2.0

            def inside(cx, cy):
                d = math.hypot(cx - ccx, cy - ccy)
                return (r - half) <= d <= (r + half)

            self._fill_bbox(ccx - r - half, ccy - r - half,
                            ccx + r + half, ccy + r + half,
                            rgb, item["opacity"], inside)

    def draw_line(self, item):
        rgb = _hex_to_rgb(item["color"])
        x1, y1, x2, y2 = item["x1"], item["y1"], item["x2"], item["y2"]
        half = item["line_width"] / 2.0
        vx, vy = x2 - x1, y2 - y1
        length2 = vx * vx + vy * vy

        def inside(cx, cy):
            if length2 == 0.0:
                return math.hypot(cx - x1, cy - y1) <= half
            t = ((cx - x1) * vx + (cy - y1) * vy) / length2
            t = max(0.0, min(1.0, t))
            qx, qy = x1 + t * vx, y1 + t * vy
            return math.hypot(cx - qx, cy - qy) <= half

        self._fill_bbox(min(x1, x2) - half, min(y1, y2) - half,
                        max(x1, x2) + half, max(y1, y2) + half,
                        rgb, item["opacity"], inside)

    def render(self, items):
        for item in items:
            if item["type"] == "rect":
                self.draw_rect(item)
            elif item["type"] == "circle":
                self.draw_circle(item)
            else:
                self.draw_line(item)
        return self.pixels


# --------------------------------------------------------------------------- #
# PNG 编码（8-bit RGB，无第三方依赖）
# --------------------------------------------------------------------------- #
def _png_chunk(tag, payload):
    chunk = tag + payload
    return struct.pack(">I", len(payload)) + chunk + struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF)


def png_bytes(width, height, pixels):
    raw = bytearray()
    stride = width * 3
    for row in range(height):
        raw.append(0)  # filter type 0：None
        raw.extend(pixels[row * stride:(row + 1) * stride])
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # bit depth 8, color type 2(RGB)
    data = b"\x89PNG\r\n\x1a\n"
    data += _png_chunk(b"IHDR", ihdr)
    data += _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    data += _png_chunk(b"IEND", b"")
    return data


def write_png(path, width, height, pixels):
    with open(path, "wb") as fh:
        fh.write(png_bytes(width, height, pixels))


def _atomic_replace(temp_path, final_path):
    """同目录临时文件 + os.replace，保证最终文件要么完整出现、要么完全不出现。"""
    os.replace(temp_path, final_path)


def _stage_bytes(target_dir, data):
    fd, tmp = tempfile.mkstemp(dir=target_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return tmp


def _stage_text(target_dir, text):
    return _stage_bytes(target_dir, text.encode("utf-8"))


# --------------------------------------------------------------------------- #
# SVG 生成（保持图元顺序，标准属性表达颜色/线宽/透明度）
# --------------------------------------------------------------------------- #
def _fmt(value):
    if float(value).is_integer():
        return str(int(value))
    return ("%.6f" % value).rstrip("0").rstrip(".")


def build_svg(scene):
    width, height = scene["width"], scene["height"]
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d">'
        % (width, height, width, height),
        '  <rect x="0" y="0" width="%d" height="%d" fill="%s"/>'
        % (width, height, scene["background"]),
    ]
    for item in scene["items"]:
        opacity = _fmt(item["opacity"])
        if item["type"] == "rect":
            if item["line_width"] is None:
                lines.append(
                    '  <rect x="%s" y="%s" width="%s" height="%s" fill="%s" fill-opacity="%s"/>'
                    % (_fmt(item["x"]), _fmt(item["y"]), _fmt(item["width"]),
                       _fmt(item["height"]), item["color"], opacity))
            else:
                lines.append(
                    '  <rect x="%s" y="%s" width="%s" height="%s" fill="none" stroke="%s" '
                    'stroke-width="%s" stroke-opacity="%s"/>'
                    % (_fmt(item["x"]), _fmt(item["y"]), _fmt(item["width"]),
                       _fmt(item["height"]), item["color"], _fmt(item["line_width"]), opacity))
        elif item["type"] == "circle":
            if item["line_width"] is None:
                lines.append(
                    '  <circle cx="%s" cy="%s" r="%s" fill="%s" fill-opacity="%s"/>'
                    % (_fmt(item["cx"]), _fmt(item["cy"]), _fmt(item["radius"]),
                       item["color"], opacity))
            else:
                lines.append(
                    '  <circle cx="%s" cy="%s" r="%s" fill="none" stroke="%s" '
                    'stroke-width="%s" stroke-opacity="%s"/>'
                    % (_fmt(item["cx"]), _fmt(item["cy"]), _fmt(item["radius"]),
                       item["color"], _fmt(item["line_width"]), opacity))
        else:
            lines.append(
                '  <line x1="%s" y1="%s" x2="%s" y2="%s" stroke="%s" stroke-width="%s" '
                'stroke-opacity="%s"/>'
                % (_fmt(item["x1"]), _fmt(item["y1"]), _fmt(item["x2"]), _fmt(item["y2"]),
                   item["color"], _fmt(item["line_width"]), opacity))
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# 文本报告
# --------------------------------------------------------------------------- #
def build_report(scene, skips):
    lines = [
        "矢量画面渲染报告",
        "=" * 24,
        "画布宽度：%d" % scene["width"],
        "画布高度：%d" % scene["height"],
        "背景色：%s" % scene["background"],
        "成功渲染图元数：%d" % len(scene["items"]),
        "跳过图元数：%d" % len(skips),
        "",
    ]
    if skips:
        lines.append("跳过明细（索引为图元在 primitives 列表中的下标，从 0 开始）：")
        for skip in skips:
            lines.append("  - 索引 %d：%s" % (skip["index"], skip["reason"]))
    else:
        lines.append("跳过明细：无")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# CLI 入口
# --------------------------------------------------------------------------- #
def _ensure_output_dir(output_path):
    parent = os.path.dirname(os.path.abspath(output_path))
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as exc:
        raise SceneError("无法创建输出目录 %s：%s" % (parent, exc)) from exc


def render(input_path, output_path):
    """供程序化调用/测试复用；成功返回 (scene, skips)，失败抛 SceneError。"""
    ext = os.path.splitext(output_path)[1].lower()
    if ext not in SUPPORTED_FORMATS:
        raise SceneError("不支持的输出格式：%s（仅支持 .png / .svg）" % ext)

    data = load_scene(input_path)          # 文件不存在 / 非法 JSON
    scene, skips = prepare_scene(data)     # 字段与画布尺寸校验（越界在此拦截）

    _ensure_output_dir(output_path)        # 多级目录自动创建
    report_path = os.path.splitext(output_path)[0] + ".txt"
    target_dir = os.path.dirname(os.path.abspath(output_path))

    # 先在目标目录生成两个临时文件，全部成功后再 os.replace 原子落位；
    # 中途任何失败都清理临时文件，绝不留下半成品图像或报告。
    staged = []
    try:
        if ext == ".png":
            raster = Rasterizer(scene["width"], scene["height"], scene["background"])
            pixels = raster.render(scene["items"])
            img_tmp = _stage_bytes(target_dir,
                                   png_bytes(scene["width"], scene["height"], pixels))
        else:
            img_tmp = _stage_text(target_dir, build_svg(scene))
        staged.append((img_tmp, output_path))

        report_tmp = _stage_text(target_dir, build_report(scene, skips))
        staged.append((report_tmp, report_path))

        for tmp, final in staged:
            _atomic_replace(tmp, final)
    except BaseException:
        for tmp, _ in staged:
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise

    return scene, skips


def main(argv=None):
    # 重定向到管道时 Windows 默认按区域编码（如 GBK）输出，统一重配置为 UTF-8，
    # 保证中文错误信息在任何调用方式下编码确定；真实控制台走宽字符 API，不受影响。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    parser = argparse.ArgumentParser(
        description="把二维矢量画面 JSON 渲染为 PNG/SVG，并生成同名 .txt 报告（纯标准库）")
    parser.add_argument("input", help="输入场景 JSON 文件路径")
    parser.add_argument("output", help="输出图像路径（扩展名决定 .png 或 .svg）")
    args = parser.parse_args(argv)

    try:
        scene, skips = render(args.input, args.output)
    except SceneError as exc:
        sys.stderr.write("错误：%s\n" % exc)
        return 1

    print("渲染完成：%s" % args.output)
    print("报告文件：%s" % (os.path.splitext(args.output)[0] + ".txt"))
    print("成功 %d 个图元，跳过 %d 个图元" % (len(scene["items"]), len(skips)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
