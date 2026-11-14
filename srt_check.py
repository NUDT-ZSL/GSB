#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""srt_check — SRT 字幕与固定帧率无声视频适配核对工具。

读取标准 SRT 字幕文件与 JSON 画幅配置，输出逐条帧号区间、行数、
边距与冲突清单（check_report.json），并渲染代表帧预览 PNG 供人工核对。

纯本地运行：不依赖网络、媒体解码器或外部服务。
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import unicodedata
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

MAX_ENTRIES = 500
DEFAULT_FONT_SIZE = 48
DEFAULT_CHARS_PER_LINE = 18
DEFAULT_BG_COLOR = "#000000"
DEFAULT_TEXT_COLOR = "#FFFFFF"
SAFE_AREA_COLOR = "#808080"

TIMECODE_RE = re.compile(
    r"^\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})\s*$"
)


class ConfigError(Exception):
    """配置缺失或非法。"""


class SrtError(Exception):
    """SRT 文件缺失或无法解析。"""


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

def load_config(path: Path) -> dict:
    if not path.is_file():
        raise ConfigError(f"配置文件不存在: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"配置文件不是合法 JSON: {path} ({exc})") from exc
    if not isinstance(raw, dict):
        raise ConfigError("配置根节点必须是 JSON 对象")

    cfg = dict(raw)

    def require_number(name, lo=None, hi=None, integer=False):
        if name not in cfg:
            raise ConfigError(f"配置缺少必要字段: {name}")
        v = cfg[name]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ConfigError(f"配置字段 {name} 数值不合法: {v!r}（应为数值）")
        if integer and not float(v).is_integer():
            raise ConfigError(f"配置字段 {name} 数值不合法: {v!r}（应为整数）")
        if lo is not None and not v > lo:
            raise ConfigError(f"配置字段 {name} 数值不合法: {v!r}（应大于 {lo}）")
        if hi is not None and not v < hi:
            raise ConfigError(f"配置字段 {name} 数值不合法: {v!r}（应小于 {hi}）")
        return int(v) if integer else v

    cfg["frame_rate"] = require_number("frame_rate", lo=0)
    cfg["width"] = require_number("width", lo=0, integer=True)
    cfg["height"] = require_number("height", lo=0, integer=True)
    cfg["margin_ratio"] = require_number("margin_ratio", lo=-1e-12, hi=0.5)
    if cfg["margin_ratio"] < 0:
        raise ConfigError(f"配置字段 margin_ratio 数值不合法: {cfg['margin_ratio']!r}（应 >= 0）")

    # 可选字段
    font_size = cfg.get("font_size", DEFAULT_FONT_SIZE)
    if isinstance(font_size, bool) or not isinstance(font_size, (int, float)) or font_size <= 0:
        raise ConfigError(f"配置字段 font_size 数值不合法: {font_size!r}（应为正数）")
    cfg["font_size"] = int(font_size)

    cpl = cfg.get("chars_per_line", DEFAULT_CHARS_PER_LINE)
    if isinstance(cpl, bool) or not isinstance(cpl, int) or cpl <= 0:
        raise ConfigError(f"配置字段 chars_per_line 数值不合法: {cpl!r}（应为正整数）")
    cfg["chars_per_line"] = cpl

    for color_field, default in (("background_color", DEFAULT_BG_COLOR),
                                 ("text_color", DEFAULT_TEXT_COLOR)):
        v = cfg.get(color_field, default)
        if not isinstance(v, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", v):
            raise ConfigError(
                f"配置字段 {color_field} 不合法: {v!r}（应为 #RRGGBB 形式）")
        cfg[color_field] = v.upper()

    font_path = cfg.get("font_path")
    if font_path is not None and not isinstance(font_path, str):
        raise ConfigError(f"配置字段 font_path 不合法: {font_path!r}（应为字符串）")
    cfg["font_path"] = font_path

    return cfg


# --------------------------------------------------------------------------- #
# SRT 解析
# --------------------------------------------------------------------------- #

def _tc_to_ms(h, m, s, ms):
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms)


def parse_srt(path: Path) -> list[dict]:
    if not path.is_file():
        raise SrtError(f"SRT 文件不存在: {path}")
    try:
        content = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        try:
            content = path.read_text(encoding="gbk")
        except UnicodeDecodeError as exc:
            raise SrtError(f"SRT 文件编码无法识别: {path}") from exc

    blocks = re.split(r"\r?\n\s*\r?\n", content.strip()) if content.strip() else []
    entries = []
    for block in blocks:
        lines = [ln for ln in block.strip().splitlines()]
        if len(lines) < 2:
            raise SrtError(f"SRT 解析失败，块格式不完整: {block[:40]!r}")
        try:
            index = int(lines[0].strip())
        except ValueError as exc:
            raise SrtError(f"SRT 解析失败，序号非法: {lines[0]!r}") from exc
        m = TIMECODE_RE.match(lines[1])
        if not m:
            raise SrtError(f"SRT 解析失败，时间码非法: {lines[1]!r}")
        start_ms = _tc_to_ms(*m.group(1, 2, 3, 4))
        end_ms = _tc_to_ms(*m.group(5, 6, 7, 8))
        text = "\n".join(lines[2:])
        entries.append({
            "index": index,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "text": text,
        })

    if not entries:
        raise SrtError(f"SRT 文件完全无法解析（无任何字幕条目）: {path}")
    if len(entries) > MAX_ENTRIES:
        raise SrtError(f"SRT 条目数 {len(entries)} 超过上限 {MAX_ENTRIES}: {path}")
    return entries


# --------------------------------------------------------------------------- #
# 核对逻辑
# --------------------------------------------------------------------------- #

def ms_to_frame(ms: int, fps: float) -> int:
    """毫秒时间码换算为帧号（四舍五入，半数向上）。"""
    return int(math.floor(ms * fps / 1000.0 + 0.5))


def char_units(ch: str) -> float:
    """全角字符计 1 单位，其余计 0.5 单位。"""
    return 1.0 if unicodedata.east_asian_width(ch) in ("F", "W") else 0.5


def estimate_lines(text: str, chars_per_line: int) -> tuple[int, float]:
    """按每行最多 chars_per_line 个全角字符估算行数，返回 (行数, 最宽行单位数)。"""
    total_lines = 0
    max_units = 0.0
    for raw_line in text.split("\n"):
        units = sum(char_units(c) for c in raw_line)
        max_units = max(max_units, units)
        total_lines += max(1, math.ceil(units / chars_per_line))
    return total_lines, max_units


def check_entries(entries: list[dict], cfg: dict) -> tuple[list[dict], list[dict]]:
    fps = cfg["frame_rate"]
    width = cfg["width"]
    margin_px = round(width * cfg["margin_ratio"])
    safe_width = width - 2 * margin_px
    safe_height = cfg["height"] - 2 * round(cfg["height"] * cfg["margin_ratio"])

    results = []
    for e in entries:
        text = e["text"]
        res = {
            "index": e["index"],
            "start_frame": None,
            "end_frame": None,
            "frame_count": None,
            "lines": None,
            "margin_left": margin_px,
            "margin_right": margin_px,
            "margin_ok": None,
            "overflow_px": None,
            "status": "ok",
            "reasons": [],
        }
        if not text.strip():
            res["status"] = "invalid"
            res["reasons"].append("文本为空或仅含空白字符")
            results.append(res)
            continue

        start_frame = ms_to_frame(e["start_ms"], fps)
        end_frame = ms_to_frame(e["end_ms"], fps) - 1  # 起止帧均包含在显示区间内
        if end_frame < start_frame:
            res["status"] = "invalid"
            res["reasons"].append(
                f"结束帧不晚于开始帧（时间倒置或零时长: {e['start_ms']}ms -> {e['end_ms']}ms）")
            results.append(res)
            continue

        lines, max_units = estimate_lines(text, cfg["chars_per_line"])
        text_width = round(max_units * cfg["font_size"])
        res.update({
            "start_frame": start_frame,
            "end_frame": end_frame,
            "frame_count": end_frame - start_frame + 1,
            "lines": lines,
            "margin_ok": text_width <= safe_width,
        })
        if text_width > safe_width:
            res["overflow_px"] = text_width - safe_width
            res["status"] = "overflow"
            res["reasons"].append(
                f"文本估算宽度 {text_width}px 超出安全区宽度 {safe_width}px，"
                f"超出 {res['overflow_px']}px")
        results.append(res)

    # 重叠检测（仅针对帧号有效的条目）
    overlaps = []
    framed = [r for r in results if r["start_frame"] is not None]
    for i in range(len(framed)):
        for j in range(i + 1, len(framed)):
            a, b = framed[i], framed[j]
            ov = min(a["end_frame"], b["end_frame"]) - max(a["start_frame"], b["start_frame"]) + 1
            if ov > 0:
                overlaps.append({
                    "indices": [a["index"], b["index"]],
                    "overlap_frames": ov,
                })

    # 按开始帧升序排列；无帧号的 invalid 条目按序号排在末尾
    results.sort(key=lambda r: (r["start_frame"] is None,
                                r["start_frame"] if r["start_frame"] is not None else 0,
                                r["index"]))
    return results, overlaps, {"safe_width": safe_width, "safe_height": safe_height,
                               "margin_px": margin_px}


# --------------------------------------------------------------------------- #
# 预览渲染
# --------------------------------------------------------------------------- #

_CJK_FONT_CANDIDATES = ["msyh.ttc", "simhei.ttf", "simsun.ttc",
                        "NotoSansCJK-Regular.ttc", "wqy-zenhei.ttc"]


def load_font(cfg: dict) -> ImageFont.FreeTypeFont:
    size = cfg["font_size"]
    candidates = [cfg["font_path"]] if cfg.get("font_path") else []
    candidates += _CJK_FONT_CANDIDATES
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def wrap_text(text: str, chars_per_line: int) -> list[str]:
    """与行数估算一致的确定性折行。"""
    out = []
    for raw_line in text.split("\n"):
        current, units = [], 0.0
        for ch in raw_line:
            u = char_units(ch)
            if current and units + u > chars_per_line:
                out.append("".join(current))
                current, units = [], 0.0
            current.append(ch)
            units += u
        out.append("".join(current))
    return out


def render_preview(entry: dict, cfg: dict, font, out_path: Path) -> None:
    width, height = cfg["width"], cfg["height"]
    margin_x = round(width * cfg["margin_ratio"])
    margin_y = round(height * cfg["margin_ratio"])
    img = Image.new("RGB", (width, height), cfg["background_color"])
    draw = ImageDraw.Draw(img)
    # 安全区边框
    draw.rectangle([margin_x, margin_y, width - margin_x - 1, height - margin_y - 1],
                   outline=SAFE_AREA_COLOR, width=2)
    # 文本（安全区内水平居中、底部对齐）
    lines = wrap_text(entry["text"], cfg["chars_per_line"])
    line_height = int(cfg["font_size"] * 1.3)
    total_h = line_height * len(lines)
    y = height - margin_y - total_h
    for line in lines:
        line_w = draw.textlength(line, font=font)
        draw.text(((width - line_w) / 2, y), line, font=font, fill=cfg["text_color"])
        y += line_height
    img.save(out_path, format="PNG")


def select_preview_entries(entries: list[dict], results: list[dict],
                           overlaps: list[dict], max_previews: int) -> list[dict]:
    """选取代表帧：首条、末条与存在问题的字幕（超宽 / 重叠），最多 max_previews 条。"""
    by_index = {e["index"]: e for e in entries}
    renderable = {}  # index -> result，仅帧号有效且文本非空的条目可渲染
    for r in results:
        if r["start_frame"] is not None:
            renderable[r["index"]] = r
    if not renderable:
        return []

    ordered = sorted(renderable)
    first, last = ordered[0], ordered[-1]
    problem_indices = set()
    for r in renderable.values():
        if r["status"] == "overflow":
            problem_indices.add(r["index"])
    for ov in overlaps:
        problem_indices.update(i for i in ov["indices"] if i in renderable)

    picked = [first]
    for idx in sorted(problem_indices):
        if idx not in picked:
            picked.append(idx)
    if last not in picked:
        picked.append(last)
    picked = picked[:max_previews]

    selected = []
    for idx in picked:
        e = dict(by_index[idx])
        e["start_frame"] = renderable[idx]["start_frame"]
        selected.append(e)
    return selected


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def run(srt_path: Path, config_path: Path, out_dir: Path, max_previews: int) -> int:
    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    try:
        entries = parse_srt(srt_path)
    except SrtError as exc:
        print(f"SRT 错误: {exc}", file=sys.stderr)
        return 3

    results, overlaps, geom = check_entries(entries, cfg)

    out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "srt_file": str(srt_path),
        "config": {
            "frame_rate": cfg["frame_rate"],
            "width": cfg["width"],
            "height": cfg["height"],
            "margin_ratio": cfg["margin_ratio"],
            "font_size": cfg["font_size"],
            "chars_per_line": cfg["chars_per_line"],
            "background_color": cfg["background_color"],
            "text_color": cfg["text_color"],
        },
        "safe_area": {
            "width": geom["safe_width"],
            "height": geom["safe_height"],
            "margin_left": geom["margin_px"],
            "margin_right": geom["margin_px"],
        },
        "entry_count": len(results),
        "entries": results,
        "overlaps": overlaps,
    }
    report_path = out_dir / "check_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")

    font = load_font(cfg)
    previews = select_preview_entries(entries, results, overlaps, max_previews)
    preview_files = []
    for e in previews:
        name = f"preview_seq{e['index']:03d}_frame{e['start_frame']:06d}.png"
        render_preview(e, cfg, font, out_dir / name)
        preview_files.append(name)

    n_invalid = sum(1 for r in results if r["status"] == "invalid")
    n_overflow = sum(1 for r in results if r["status"] == "overflow")
    print(f"核对完成: 共 {len(results)} 条字幕, "
          f"invalid {n_invalid} 条, 超宽 {n_overflow} 条, 重叠 {len(overlaps)} 组")
    print(f"核对报告: {report_path}")
    for name in preview_files:
        print(f"预览帧: {out_dir / name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="srt_check",
        description="SRT 字幕与固定帧率无声视频适配核对工具：输出逐条帧号区间、"
                    "行数、边距与冲突清单（check_report.json），并渲染代表帧预览 PNG。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--srt", required=True, type=Path,
                   help="SRT 字幕文件路径（单文件不超过 500 条）")
    p.add_argument("--config", required=True, type=Path,
                   help="JSON 配置路径，指定 frame_rate / width / height / margin_ratio，"
                        "可选 font_size / chars_per_line / font_path / "
                        "background_color / text_color")
    p.add_argument("--out-dir", required=True, type=Path,
                   help="输出目录，存放 check_report.json 与预览 PNG")
    p.add_argument("--max-previews", type=int, default=5,
                   help="最多渲染的代表帧预览 PNG 数量")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_previews < 0:
        print("参数错误: --max-previews 不能为负数", file=sys.stderr)
        return 2
    return run(args.srt, args.config, args.out_dir, args.max_previews)


if __name__ == "__main__":
    sys.exit(main())
