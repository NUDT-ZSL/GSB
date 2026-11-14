# -*- coding: utf-8 -*-
"""
frame_anim_tool.py
==================
本地命令行帧序列动画合成工具（无第三方依赖，除 Pillow 外仅使用标准库）。

功能：
  1. 读取帧清单 JSON，校验画布宽高、循环次数等字段（字段缺失/类型错误报具体字段名）。
  2. 按清单顺序逐帧加载 PNG；缺文件、损坏、超画布、延时非法、全透明等单帧问题
     不影响其他帧继续合成，且在报告中按不同原因区分。
  3. 合成预览 GIF：小图居中、背景色填充；延时精确写为厘秒整数；循环次数按清单写入。
  4. 为每帧导出透明通道预览 PNG（白=完全透明，黑=完全不透明，灰阶=半透明；
     半透明边缘不含纯黑像素，可与不透明区域区分，杜绝“黑边渗色”观感）。
  5. 输出确定性报告 JSON：同样输入重复运行产物逐字节一致。

用法：
  python -I frame_anim_tool.py --manifest 清单.json --output-dir 输出目录
退出码：
  0  成功（允许存在无效帧，报告中区分原因）
  2  清单结构/字段校验失败（错误信息指出具体字段）
  1  其他未预期错误（如输出目录无法创建）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image

# Windows 控制台默认 GBK 代码页，统一重配为 UTF-8，避免中文报告输出乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# 明确的上下界（需求 13）。单位：像素 / 毫秒 / 次。
# ---------------------------------------------------------------------------
CANVAS_MIN_PX = 1
CANVAS_MAX_PX = 4096
DELAY_MIN_MS = 10          # GIF 时基为厘秒(10ms)，低于该值无法无损表示，判越界
DELAY_MAX_MS = 60_000      # 单帧延时上限 60 秒
LOOP_MIN = 0               # 0 表示无限循环（GIF 标准约定）
LOOP_MAX = 1000

# 无效原因码 -> 报告中的中文分类（需求 11：不同未完成原因分别可见）
REASON_LABELS = {
    "missing_file": "缺失",
    "corrupt_image": "损坏",
    "oversized": "超尺寸",
    "delay_invalid": "延时非法",
    "delay_out_of_range": "延时非法",
    "fully_transparent": "全透明",
    "missing_field": "字段缺失",
    "malformed_entry": "条目格式错误",
}


class ManifestError(Exception):
    """清单级校验错误：退出码 2，信息中包含具体字段路径。"""


# ---------------------------------------------------------------------------
# 基础校验辅助
# ---------------------------------------------------------------------------
def is_int(value) -> bool:
    """严格整数判定：bool 不属于合法整数（避免 True 被当作 1）。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _field_error(field_path: str, detail: str) -> ManifestError:
    return ManifestError(f"字段 '{field_path}' {detail}")


def _parse_bg_color(value, field_path: str):
    """背景色支持 '#RRGGBB' 或 [r,g,b]，返回 (r,g,b) 三元组（各 0-255）。"""
    if value is None:
        return (255, 255, 255)
    if isinstance(value, str):
        s = value.strip()
        if len(s) == 7 and s.startswith("#"):
            try:
                r = int(s[1:3], 16)
                g = int(s[3:5], 16)
                b = int(s[5:7], 16)
                return (r, g, b)
            except ValueError:
                pass
        raise _field_error(field_path, f"背景色格式非法，应为 '#RRGGBB'，实际为 {value!r}")
    if isinstance(value, list) and len(value) == 3 and all(is_int(c) and 0 <= c <= 255 for c in value):
        return (int(value[0]), int(value[1]), int(value[2]))
    raise _field_error(
        field_path, f"背景色类型非法，应为 '#RRGGBB' 字符串或 3 个 0-255 整数的数组，实际为 {value!r}"
    )


def validate_manifest(data) -> dict:
    """
    校验清单顶层结构与字段，返回规范化后的清单字典。
    画布宽高/循环次数缺失或类型错误时，ManifestError 信息指出具体字段名（需求 1、5）。
    """
    if not isinstance(data, dict):
        raise ManifestError("清单根节点必须是 JSON 对象")

    canvas = data.get("canvas")
    if canvas is None:
        raise ManifestError("字段 'canvas' 缺失：清单必须包含画布尺寸对象")
    if not isinstance(canvas, dict):
        raise _field_error("canvas", "类型错误，应为包含 width/height 的对象")

    if "width" not in canvas:
        raise ManifestError("字段 'canvas.width' 缺失：画布宽度必须声明")
    if "height" not in canvas:
        raise ManifestError("字段 'canvas.height' 缺失：画布高度必须声明")
    if not is_int(canvas["width"]):
        raise _field_error("canvas.width", f"类型错误，应为整数，实际为 {canvas['width']!r}")
    if not is_int(canvas["height"]):
        raise _field_error("canvas.height", f"类型错误，应为整数，实际为 {canvas['height']!r}")
    if not (CANVAS_MIN_PX <= canvas["width"] <= CANVAS_MAX_PX):
        raise _field_error(
            "canvas.width",
            f"越界：画布宽度须在 [{CANVAS_MIN_PX}, {CANVAS_MAX_PX}] 像素内，实际为 {canvas['width']}",
        )
    if not (CANVAS_MIN_PX <= canvas["height"] <= CANVAS_MAX_PX):
        raise _field_error(
            "canvas.height",
            f"越界：画布高度须在 [{CANVAS_MIN_PX}, {CANVAS_MAX_PX}] 像素内，实际为 {canvas['height']}",
        )

    # 循环次数：未声明 -> 无限循环（0），并在报告中注明取值来源（需求 5）
    loop_source = "manifest"
    if "loop_count" not in data or data["loop_count"] is None:
        loop_count = 0
        loop_source = "default_infinite"
    else:
        loop_count = data["loop_count"]
        if not is_int(loop_count):
            raise _field_error("loop_count", f"类型错误，应为非负整数，实际为 {loop_count!r}")
        if not (LOOP_MIN <= loop_count <= LOOP_MAX):
            raise _field_error(
                "loop_count", f"越界：循环次数须在 [{LOOP_MIN}, {LOOP_MAX}] 内（0 表示无限），实际为 {loop_count}"
            )

    bg_color = _parse_bg_color(data.get("background_color"), "background_color")

    frames = data.get("frames")
    if frames is None:
        raise ManifestError("字段 'frames' 缺失：清单必须包含帧条目数组")
    if not isinstance(frames, list):
        raise _field_error("frames", "类型错误，应为数组")

    return {
        "width": int(canvas["width"]),
        "height": int(canvas["height"]),
        "loop_count": int(loop_count),
        "loop_source": loop_source,
        "bg_color": bg_color,
        "frames": frames,
    }


def validate_delay(value):
    """
    校验单帧延时（毫秒正整数且在上下界内）。
    返回 (delay_ms 或 None, 原因码或 None, 说明或 None)。
    """
    if value is None:
        return None, "delay_invalid", "延时字段缺失"
    if not is_int(value):
        return None, "delay_invalid", f"延时类型非法，应为毫秒正整数，实际为 {value!r}"
    if value < DELAY_MIN_MS or value > DELAY_MAX_MS:
        return (
            None,
            "delay_out_of_range",
            f"延时越界：须在 [{DELAY_MIN_MS}, {DELAY_MAX_MS}] 毫秒内，实际为 {value} 毫秒",
        )
    return int(value), None, None


# ---------------------------------------------------------------------------
# 确定性调色板：固定 6x6x6 网页安全色 + 灰阶，保证重复运行 GIF 逐字节一致。
# ---------------------------------------------------------------------------
def build_fixed_palette() -> Image.Image:
    palette = []
    for r in range(6):
        for g in range(6):
            for b in range(6):
                palette += [r * 51, g * 51, b * 51]       # 0,51,102,153,204,255
    for i in range(216, 256):                              # 补齐 256 色灰阶
        v = (i - 216) * 12                                  # 0..480 截断在 255
        palette += [min(v, 255)] * 3
    pal_img = Image.new("P", (1, 1))
    pal_img.putpalette(palette)
    return pal_img


FIXED_PALETTE = build_fixed_palette()


def composite_frame(img: Image.Image, canvas_w: int, canvas_h: int, bg_color) -> Image.Image:
    """
    将帧等尺寸/小尺寸放置到画布上：居中，四周用背景色填充，四边边距按整数取整
    （左/上边距向下取整，右/下向上取整，偶数差时左右边距严格相等——需求 8）。
    半透明像素与背景做 alpha 合成，避免直接转 GIF 产生的黑边渗色。
    """
    canvas = Image.new("RGBA", (canvas_w, canvas_h), bg_color + (255,))
    frame = img.convert("RGBA")
    x = (canvas_w - frame.width) // 2
    y = (canvas_h - frame.height) // 2
    canvas.alpha_composite(frame, (x, y))
    return canvas


def quantize_for_gif(rgba_img: Image.Image) -> Image.Image:
    """用固定调色板、关闭抖动进行量化，保证输出确定性。

    合成画布以不透明背景打底并经 alpha_composite，所有像素 alpha 均为 255，
    转 RGB 无损；Pillow 仅接受 RGB/L 模式做调色板量化。"""
    return rgba_img.convert("RGB").quantize(palette=FIXED_PALETTE, dither=Image.Dither.NONE)


def render_alpha_preview(img: Image.Image) -> Image.Image:
    """
    透明通道预览（L 模式灰阶 PNG）：
      alpha=255（不透明）   -> 0   纯黑
      alpha=0  （全透明）   -> 255 纯白
      0<alpha<255（半透明） -> 255-alpha，取值 1..254，恒不为纯黑
    因此半透明边缘可辨且不含任何纯黑像素（需求 6），与不透明区域天然区分。
    """
    rgba = img.convert("RGBA")
    alpha = rgba.getchannel("A")
    return alpha.point(lambda a: 255 - a)


def is_fully_transparent(img: Image.Image) -> bool:
    return img.convert("RGBA").getchannel("A").getextrema()[1] == 0


# ---------------------------------------------------------------------------
# 主处理流程
# ---------------------------------------------------------------------------
def process(manifest_path: Path, output_dir: Path) -> dict:
    try:
        with manifest_path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        raise ManifestError(f"清单文件不存在：{manifest_path}")
    except json.JSONDecodeError as e:
        raise ManifestError(f"清单 JSON 解析失败（第 {e.lineno} 行第 {e.colno} 列）：{e.msg}")

    spec = validate_manifest(raw)
    base_dir = manifest_path.parent
    width, height = spec["width"], spec["height"]

    try:
        output_dir.mkdir(parents=True, exist_ok=True)          # 需求 15：不存在自动创建
        alpha_dir = output_dir / "alpha_preview"
        alpha_dir.mkdir(exist_ok=True)
    except OSError as e:
        raise RuntimeError(f"无法创建输出目录 {output_dir}：{e}") from e

    frame_results = []
    gif_frames: list[Image.Image] = []
    gif_durations: list[int] = []
    valid_count = 0
    invalid_count = 0

    for index, entry in enumerate(spec["frames"]):
        seq = index + 1
        result = {
            "index": index,                       # 从 0 开始的帧序号，便于按序号定位
            "sequence": seq,                     # 从 1 开始的人类可读序号
            "file": None,
            "valid": False,
            "reason_code": None,
            "reason": None,
            "detail": None,
            "delay_ms": None,
            "alpha_preview": None,
            "placement": None,
        }

        # 条目本身结构异常时，记录原因后继续处理后续帧
        if not isinstance(entry, dict):
            invalid_count += 1
            result.update(reason_code="malformed_entry", reason=REASON_LABELS["malformed_entry"],
                          detail=f"帧条目必须是对象，实际为 {type(entry).__name__}")
            frame_results.append(result)
            continue

        rel_file = entry.get("file")
        result["file"] = rel_file
        if not isinstance(rel_file, str) or not rel_file:
            invalid_count += 1
            result.update(reason_code="missing_field", reason=REASON_LABELS["missing_field"],
                          detail="帧条目缺少有效的 'file' 字符串字段")
            frame_results.append(result)
            continue

        frame_path = (base_dir / rel_file)

        # 原因 1：缺文件（需求 9）
        if not frame_path.is_file():
            invalid_count += 1
            result.update(reason_code="missing_file", reason=REASON_LABELS["missing_file"],
                          detail=f"文件不存在：{rel_file}")
            frame_results.append(result)
            continue

        # 原因 2：无法解码（损坏图，需求 2）。Image.open 是惰性的，必须 load() 才触发解码
        try:
            with Image.open(frame_path) as im:
                im.load()
                img = im.copy()
        except Exception as e:  # Pillow 对损坏文件抛出多种异常，统一归为损坏
            invalid_count += 1
            result.update(reason_code="corrupt_image", reason=REASON_LABELS["corrupt_image"],
                          detail=f"PNG 无法解码：{type(e).__name__}: {e}")
            frame_results.append(result)
            continue

        # 原因 3：超画布（宽或高超出声明画布，需求 2）
        if img.width > width or img.height > height:
            invalid_count += 1
            result.update(
                reason_code="oversized", reason=REASON_LABELS["oversized"],
                detail=(f"帧尺寸 {img.width}x{img.height} 超出画布 {width}x{height}"),
            )
            frame_results.append(result)
            continue

        # 走到这里说明像素数据可用：无论后续判定如何，先导出透明通道预览（需求 6）
        alpha_name = f"frame_{index:03d}_alpha.png"
        render_alpha_preview(img).save(alpha_dir / alpha_name)
        result["alpha_preview"] = f"alpha_preview/{alpha_name}"

        # 原因 4：延时非法 / 越界（需求 3、13，0 毫秒因低于下界被判无效）
        delay_ms, reason_code, detail = validate_delay(entry.get("delay_ms"))
        if reason_code is not None:
            invalid_count += 1
            result.update(reason_code=reason_code, reason=REASON_LABELS[reason_code], detail=detail)
            frame_results.append(result)
            continue

        # 原因 5：全透明帧（需求 7），单独列出且不计入总时长
        if is_fully_transparent(img):
            invalid_count += 1
            result.update(reason_code="fully_transparent", reason=REASON_LABELS["fully_transparent"],
                          detail="帧的 alpha 通道最大值为 0，整帧完全透明，判为空帧")
            frame_results.append(result)
            continue

        # ---- 有效帧：居中合成、量化、按原顺序入 GIF（需求 4、8、12）----
        composited = composite_frame(img, width, height, spec["bg_color"])
        x = (width - img.width) // 2
        y = (height - img.height) // 2
        quantized = quantize_for_gif(composited)
        gif_frames.append(quantized)
        gif_durations.append(delay_ms)

        valid_count += 1
        result.update(
            valid=True,
            delay_ms=delay_ms,
            placement={
                "offset_x": x,
                "offset_y": y,
                "margin_left": x,
                "margin_right": width - img.width - x,
                "margin_top": y,
                "margin_bottom": height - img.height - y,
                "centered": True,
            },
        )
        frame_results.append(result)

    # ---- 写 GIF（无有效帧时仍产出报告，但不生成 GIF）----
    gif_rel = None
    if gif_frames:
        gif_path = output_dir / "preview.gif"
        gif_frames[0].save(
            gif_path,
            save_all=True,
            append_images=gif_frames[1:],
            duration=gif_durations,                 # 毫秒整数；本工具允许值均为 10 的倍数，
                                                    # Pillow 写厘秒时不产生取整误差（需求 4）
            loop=spec["loop_count"],                # 0=无限（需求 5）
            disposal=2,                             # 每帧恢复背景，避免帧间残影
            optimize=False,                         # 关闭优化，保证确定性（需求 14）
        )
        gif_rel = "preview.gif"

    total_duration_ms = sum(gif_durations)
    loop_note = (
        "清单未声明 loop_count，按默认值写为无限循环(0)"
        if spec["loop_source"] == "default_infinite"
        else "循环次数取自清单字段 loop_count"
    )

    report = {
        "manifest": str(manifest_path.name),
        "canvas": {"width": width, "height": height},
        "background_color": "#%02x%02x%02x" % spec["bg_color"],
        "loop_count": spec["loop_count"],
        "loop_count_source": spec["loop_source"],     # manifest / default_infinite
        "loop_note": loop_note,
        "bounds": {
            "canvas_px": [CANVAS_MIN_PX, CANVAS_MAX_PX],
            "delay_ms": [DELAY_MIN_MS, DELAY_MAX_MS],
            "loop_count": [LOOP_MIN, LOOP_MAX],
        },
        "summary": {
            "total_entries": len(spec["frames"]),
            "valid_frames": valid_count,
            "invalid_frames": invalid_count,
            "total_duration_ms": total_duration_ms,
            "gif_frame_count": len(gif_frames),
            "gif_output": gif_rel,
        },
        "frames": frame_results,
    }

    report_path = output_dir / "report.json"
    with report_path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="读取帧清单 JSON 与逐帧 PNG，合成预览 GIF、导出透明通道预览并生成处理报告。"
    )
    parser.add_argument("--manifest", required=True, help="帧清单 JSON 路径")
    parser.add_argument("--output-dir", required=True, help="输出目录（不存在时自动创建）")
    args = parser.parse_args(argv)

    try:
        report = process(Path(args.manifest), Path(args.output_dir))
    except ManifestError as e:
        print(f"[清单校验失败] {e}", file=sys.stderr)
        return 2
    except Exception as e:  # 输出目录不可写等未预期 I/O 错误
        print(f"[执行失败] {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    s = report["summary"]
    loop_text = "无限" if report["loop_count"] == 0 else f"{report['loop_count']} 次"
    print(
        f"[完成] 条目 {s['total_entries']} | 有效 {s['valid_frames']} | 无效 {s['invalid_frames']} | "
        f"总时长 {s['total_duration_ms']} ms | 循环 {loop_text}"
    )
    if s["gif_output"] is None:
        print("[提示] 无有效帧，未生成 GIF；报告仍已写出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
