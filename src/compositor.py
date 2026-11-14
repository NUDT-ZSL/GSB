# -*- coding: utf-8 -*-
"""离线分层画面叠加预览合成器。

按图层描述 JSON 声明的顺序（自下而上），把本地图层 PNG 以指定不透明度
叠加到画布底色上，输出最终合成 PNG 与记录 JSON。

设计要点：
- 仅使用 Pillow 10.x 即已存在的稳定 API（open/new/convert/getpixel/
  putpixel/save），不依赖高版本特性；
- 混合采用整数运算的 source-over（Porter-Duff）公式，逐像素可复核、
  跨运行逐字节确定；
- 单层问题（缺文件、无法解码、不透明度非法、坐标越界无交集等）只
  影响该层，其余层继续合成；描述级问题（文件缺失、JSON 损坏、画布或
  底色非法）使整份描述不可用，不产出成品。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from PIL import Image

# 固定输出文件名，保证落位约定与重复运行可比对。
OUTPUT_IMAGE_NAME = "composite.png"
RECORD_NAME = "record.json"

# 记录中使用的固定原因/状态文案（不允许出现时间戳等易变字段）。
REASON_NOT_OBJECT = "图层条目不是 JSON 对象，已跳过"
REASON_NAME_MISSING = "缺少图层名称(name)，已跳过"
REASON_FILE_MISSING = "缺少图层文件名(file)，已跳过"
REASON_COORD_INVALID = "坐标(x/y)缺失或不是整数，已跳过"
REASON_OPACITY_MISSING = "不透明度(opacity)缺失，已跳过"
REASON_OPACITY_RANGE = "不透明度(opacity)超出 0~1 范围，已跳过"
REASON_FILE_NOT_FOUND = "图层 PNG 文件不存在，已跳过"
REASON_FILE_DECODE = "图层 PNG 无法解码，已跳过"
REASON_NO_OVERLAP = "图层与画布无交集（内容全部落在画布外），该层无可见贡献"


def _fail(message: str) -> int:
    """描述级致命错误：打印说明并以 0 正常退出（不产出成品）。"""
    print(f"描述不可用：{message}", file=sys.stderr)
    return 0


def parse_background(value: Any) -> tuple[int, int, int, int] | None:
    """解析画布底色，支持 6 位（RGB）与 8 位（RGBA）十六进制。

    可带或不带前导 '#'；非法写法返回 None。
    """
    if not isinstance(value, str):
        return None
    text = value[1:] if value.startswith("#") else value
    if len(text) == 6:
        digits = text + "ff"
    elif len(text) == 8:
        digits = text
    else:
        return None
    # 全部分为合法十六进制字符才接受。
    if any(ch not in "0123456789abcdefABCDEF" for ch in digits):
        return None
    return (
        int(digits[0:2], 16),
        int(digits[2:4], 16),
        int(digits[4:6], 16),
        int(digits[6:8], 16),
    )


def _is_positive_int(value: Any) -> bool:
    # bool 是 int 的子类，画布尺寸不接受布尔值。
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def load_description(path: str) -> tuple[dict[str, Any] | None, str | None]:
    """读取并校验描述 JSON。

    成功返回 (描述对象, None)；描述级错误返回 (None, 原因)。
    图层相对路径以描述文件所在目录为基准解析。
    """
    if not os.path.isfile(path):
        return None, f"描述 JSON 文件不存在：{path}"
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, "描述 JSON 无法解析"
    except OSError as exc:
        return None, f"描述 JSON 读取失败：{exc}"

    if not isinstance(data, dict):
        return None, "描述顶层必须是 JSON 对象"

    width = data.get("canvas_width")
    height = data.get("canvas_height")
    if not (_is_positive_int(width) and _is_positive_int(height)):
        return None, "画布宽高(canvas_width/canvas_height)必须为正整数"

    bg = parse_background(data.get("background"))
    if bg is None:
        return None, "底色格式非法（仅支持 6 位或 8 位十六进制，如 #2b3a55 或 #2b3a55ff）"

    layers = data.get("layers")
    if not isinstance(layers, list):
        return None, "图层列表(layers)缺失或不是数组"

    data["_base_dir"] = os.path.dirname(os.path.abspath(path))
    return data, None


def composite_layer(
    canvas: Image.Image,
    layer: Image.Image,
    offset_x: int,
    offset_y: int,
    opacity: float,
) -> int:
    """把单个 RGBA 图层按整数 source-over 公式叠加到画布上。

    图层有效 alpha = 图层自身 alpha × opacity（四舍五入到整数并夹取）。
    仅处理与画布相交的区域；返回实际参与混合的像素数（0 表示无交集）。
    """
    cw, ch = canvas.size
    lw, lh = layer.size

    # 求图层矩形与画布矩形的交集（同时处理负坐标与完全越界）。
    x0 = max(offset_x, 0)
    y0 = max(offset_y, 0)
    x1 = min(offset_x + lw, cw)
    y1 = min(offset_y + lh, ch)
    if x0 >= x1 or y0 >= y1:
        return 0

    target = canvas.load()
    source = layer.load()
    mixed = 0
    for cy in range(y0, y1):
        for cx in range(x0, x1):
            sr, sg, sb, sa = source[cx - offset_x, cy - offset_y]
            # 有效源 alpha：自身透明通道与不透明度相乘，四舍五入后夹取到 0..255。
            sa = int(sa * opacity + 0.5)
            if sa > 255:
                sa = 255
            if sa == 0:
                continue
            dr, dg, db, da = target[cx, cy]
            # 整数 source-over：
            # out_a = sa + da*(255-sa)/255
            # out_c = (sc*sa + dc*da*(255-sa)/255) / out_a
            inv = 255 - sa
            out_a = sa + (da * inv + 127) // 255
            if out_a == 0:
                continue
            out_r = (sr * sa + (dr * da * inv + 127) // 255 + out_a // 2) // out_a
            out_g = (sg * sa + (dg * da * inv + 127) // 255 + out_a // 2) // out_a
            out_b = (sb * sa + (db * da * inv + 127) // 255 + out_a // 2) // out_a
            target[cx, cy] = (
                max(0, min(255, out_r)),
                max(0, min(255, out_g)),
                max(0, min(255, out_b)),
                max(0, min(255, out_a)),
            )
            mixed += 1
    return mixed


def _entry_base(layer: Any) -> dict[str, Any]:
    """记录条目的公共字段，原样回显声明值，非法/缺失时为 null。"""
    if not isinstance(layer, dict):
        return {"name": None, "x": None, "y": None, "opacity": None}
    return {
        "name": layer.get("name"),
        "x": layer.get("x"),
        "y": layer.get("y"),
        "opacity": layer.get("opacity"),
    }


def run(description_path: str, output_dir: str) -> int:
    """主流程：校验描述 -> 逐层叠加 -> 写成品与记录。返回进程退出码。"""
    data, error = load_description(description_path)
    if error is not None:
        return _fail(error)

    width: int = data["canvas_width"]
    height: int = data["canvas_height"]
    background = parse_background(data["background"])  # 入口处已校验
    assert background is not None
    base_dir: str = data["_base_dir"]

    # 描述合法后才创建输出目录（含不存在的嵌套目录）。
    os.makedirs(output_dir, exist_ok=True)

    canvas = Image.new("RGBA", (width, height), background)
    completed: list[dict[str, Any]] = []
    incomplete: list[dict[str, Any]] = []

    for index, layer in enumerate(data["layers"]):
        entry = _entry_base(layer)

        if not isinstance(layer, dict):
            entry["reason"] = REASON_NOT_OBJECT
            incomplete.append(entry)
            continue
        if not isinstance(layer.get("name"), str) or not layer["name"]:
            entry["reason"] = REASON_NAME_MISSING
            incomplete.append(entry)
            continue
        file_name = layer.get("file")
        if not isinstance(file_name, str) or not file_name:
            entry["reason"] = REASON_FILE_MISSING
            incomplete.append(entry)
            continue
        x = layer.get("x")
        y = layer.get("y")
        if not (isinstance(x, int) and isinstance(y, int)) or isinstance(x, bool) or isinstance(y, bool):
            entry["reason"] = REASON_COORD_INVALID
            incomplete.append(entry)
            continue
        opacity = layer.get("opacity")
        if opacity is None:
            entry["reason"] = REASON_OPACITY_MISSING
            incomplete.append(entry)
            continue
        if (
            not isinstance(opacity, (int, float))
            or isinstance(opacity, bool)
            or not (0.0 <= float(opacity) <= 1.0)
        ):
            entry["reason"] = REASON_OPACITY_RANGE
            incomplete.append(entry)
            continue

        layer_path = file_name
        if not os.path.isabs(layer_path):
            layer_path = os.path.normpath(os.path.join(base_dir, file_name))
        entry["file"] = None  # 未完成条目文件名字段固定为 null。

        if not os.path.isfile(layer_path):
            entry["reason"] = REASON_FILE_NOT_FOUND
            incomplete.append(entry)
            print(f"[跳过] {layer['name']}：{REASON_FILE_NOT_FOUND}", file=sys.stderr)
            continue
        try:
            with Image.open(layer_path) as img:
                img.load()
                rgba = img.convert("RGBA")
        except Exception:  # Pillow 对损坏文件会抛多种异常，统一按无法解码处理。
            entry["reason"] = REASON_FILE_DECODE
            incomplete.append(entry)
            print(f"[跳过] {layer['name']}：{REASON_FILE_DECODE}", file=sys.stderr)
            continue

        pixel_count = composite_layer(canvas, rgba, x, y, float(opacity))
        done = {
            "name": layer["name"],
            "file": os.path.basename(layer_path),
            "x": x,
            "y": y,
            "opacity": float(opacity),
            "layer_width": rgba.width,
            "layer_height": rgba.height,
            "mixed_pixels": pixel_count,
        }
        if pixel_count == 0:
            done["note"] = REASON_NO_OVERLAP
        completed.append(done)

    # 固定键顺序、无时间戳，保证记录可逐字节复现。
    if not data["layers"]:
        status = "描述未声明任何图层，已输出纯底色成品"
    elif not completed:
        status = "无可用图层，全部图层均未完成，已输出纯底色成品"
    elif incomplete:
        status = f"合成完成：{len(completed)} 层已叠加，{len(incomplete)} 层未完成"
    else:
        status = f"合成完成：全部 {len(completed)} 层均已叠加"

    record = {
        "canvas": {"width": width, "height": height, "background": data["background"]},
        "layer_counts": {
            "declared": len(data["layers"]),
            "completed": len(completed),
            "incomplete": len(incomplete),
        },
        "status": status,
        "completed_layers": completed,
        "incomplete_layers": incomplete,
    }

    image_path = os.path.join(output_dir, OUTPUT_IMAGE_NAME)
    record_path = os.path.join(output_dir, RECORD_NAME)
    # 不写入任何元数据，压缩参数固定，保证 PNG 逐字节一致。
    canvas.save(image_path, format="PNG", compress_level=6)
    with open(record_path, "w", encoding="utf-8", newline="\n") as fp:
        json.dump(record, fp, ensure_ascii=False, indent=2, sort_keys=False)
        fp.write("\n")

    print(status)
    print(f"成品 PNG：{os.path.abspath(image_path)}")
    print(f"记录 JSON：{os.path.abspath(record_path)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="离线分层画面叠加预览合成器：按描述 JSON 叠加本地图层 PNG。"
    )
    parser.add_argument("description", help="图层描述 JSON 路径")
    parser.add_argument("output_dir", help="输出目录（不存在时自动创建，含嵌套目录）")
    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认编码可能不是 UTF-8，统一为 UTF-8 以稳定输出中文。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args.description, args.output_dir)
    except Exception as exc:  # 兜底：任何意外都正常报错退出，不向用户抛栈。
        print(f"运行失败：{exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
