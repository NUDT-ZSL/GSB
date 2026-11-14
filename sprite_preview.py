#!/usr/bin/env python3
"""精灵图集透明边缘预览工具。

读取一张带 alpha 通道的 PNG 图集和一份 JSON 区域配置，为每个有效区域
生成带棋盘格透明背景与 8 像素内边距的独立预览 PNG，并输出 JSON 摘要报告。

用法见 sprite_preview --help。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

# Windows 管道默认按区域编码（GBK）输出，统一切到 UTF-8 保证中文提示可读。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

PADDING = 8
CELL = 8
LIGHT_GRAY = (0xCC, 0xCC, 0xCC, 0xFF)
WHITE = (0xFF, 0xFF, 0xFF, 0xFF)
REPORT_NAME = "report.json"

# 报告中的区域状态值
ST_CONFIG_INVALID = "config_invalid"        # 缺字段 / 字段类型错误
ST_INVALID_COORDS = "invalid_coordinates"   # 负坐标 / 宽高非正
ST_OUT_OF_BOUNDS = "out_of_bounds"          # 超出图集边界
ST_SUPERSEDED = "superseded"                # 同名区域，已被后出现者覆盖
ST_FULLY_TRANSPARENT = "fully_transparent"  # 裁剪结果 alpha 全为 0（仍生成预览）
ST_SUCCESS = "success"                      # 正常渲染
ST_WRITE_FAILED = "write_failed"            # 预览写入失败（未完成）

REQUIRED_FIELDS = ("name", "x", "y", "width", "height")


def fail(message: str) -> "int":
    """输出错误提示并返回非零退出码。"""
    print(f"错误：{message}", file=sys.stderr)
    return 1


def load_atlas(path: Path) -> tuple[Image.Image, int, int]:
    """加载并校验图集：必须是可解析 PNG 且含 alpha 通道。

    返回 (RGBA 图像, 宽, 高)。失败时抛出 ValueError。
    """
    try:
        img = Image.open(path)
    except FileNotFoundError:
        raise ValueError(f"图集文件不存在：{path}")
    except UnidentifiedImageError:
        raise ValueError(f"文件不是可解析的 PNG 图像：{path}")
    except OSError as exc:
        raise ValueError(f"图集无法解析（可能已损坏）：{path}（{exc}）")

    if (img.format or "").upper() != "PNG":
        raise ValueError(f"图集必须为 PNG 格式，实际格式为 {img.format or '未知'}：{path}")

    # LA / PA / RGBA 等含透明度的模式统一转 RGBA；RGB、L 等无 alpha 的模式直接拒绝。
    if not (img.mode in ("RGBA", "LA", "PA") or "A" in img.getbands()):
        raise ValueError(
            f"图集不含 alpha 通道（当前模式 {img.mode}），无法进行透明边缘预览：{path}"
        )

    try:
        img.load()
        rgba = img.convert("RGBA")
    except OSError as exc:
        raise ValueError(f"图集像素数据损坏，无法解码：{path}（{exc}）")

    return rgba, rgba.width, rgba.height


def load_regions(path: Path) -> list[dict[str, Any]]:
    """加载并校验区域配置：必须是可解析的 JSON 数组。失败时抛出 ValueError。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ValueError(f"配置文件不存在：{path}")
    except OSError as exc:
        raise ValueError(f"配置文件无法读取：{path}（{exc}）")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"配置文件不是合法 JSON（第 {exc.lineno} 行第 {exc.colno} 列）：{path}")

    if not isinstance(data, list):
        raise ValueError(f"区域配置必须是 JSON 数组，实际为 {type(data).__name__}：{path}")

    return data


def sanitize_filename(name: str) -> str | None:
    """把区域名称转为安全的预览文件名。返回 None 表示名称不可用。"""
    cleaned = name.strip()
    if not cleaned:
        return None
    # Windows 保留字符与控制字符一律替换为下划线，避免路径分隔符穿越输出目录。
    for ch in '<>:"/\\|?*':
        cleaned = cleaned.replace(ch, "_")
    cleaned = "".join("_" if ord(c) < 32 else c for c in cleaned)
    cleaned = cleaned.rstrip(" .") or "_"
    if cleaned in {".", ".."}:
        return None
    return cleaned + ".png"


def validate_entry(
    entry: Any, atlas_w: int, atlas_h: int
) -> tuple[dict[str, Any] | None, str | None]:
    """校验单条配置。

    返回 (规范化区域, None) 表示通过；否则返回 (None, 状态码)。
    """
    if not isinstance(entry, dict):
        return None, ST_CONFIG_INVALID

    for field in REQUIRED_FIELDS:
        if field not in entry:
            return None, ST_CONFIG_INVALID

    name = entry["name"]
    if not isinstance(name, str) or not name.strip():
        return None, ST_CONFIG_INVALID

    coords = (entry["x"], entry["y"], entry["width"], entry["height"])
    # bool 是 int 的子类，显式排除；字符串等非整数坐标视为配置无效。
    if any(isinstance(v, bool) or not isinstance(v, int) for v in coords):
        return None, ST_CONFIG_INVALID

    x, y, w, h = coords
    if x < 0 or y < 0 or w <= 0 or h <= 0:
        return None, ST_INVALID_COORDS

    # 越界不做任何裁剪修正，直接判无效。
    if x + w > atlas_w or y + h > atlas_h:
        return None, ST_OUT_OF_BOUNDS

    return {"name": name, "x": x, "y": y, "width": w, "height": h}, None


def make_checkerboard(width: int, height: int) -> Image.Image:
    """生成 8 像素单元的棋盘格背景（浅灰 0xCC 与白 0xFF 按像素坐标交替）。"""
    board = Image.new("RGBA", (width, height))
    px = board.load()
    for py in range(height):
        for px_x in range(width):
            cell = (px_x // CELL) + (py // CELL)
            px[px_x, py] = LIGHT_GRAY if cell % 2 == 0 else WHITE
    return board


def render_preview(atlas: Image.Image, region: dict[str, Any]) -> tuple[Image.Image, bool]:
    """裁剪区域并合成棋盘格预览。返回 (预览图, 是否全透明)。"""
    crop = atlas.crop(
        (region["x"], region["y"], region["x"] + region["width"], region["y"] + region["height"])
    )
    alpha = crop.getchannel("A")
    fully_transparent = alpha.getextrema()[1] == 0  # 最大 alpha 为 0 即全透明

    preview_w = region["width"] + PADDING * 2
    preview_h = region["height"] + PADDING * 2
    preview = make_checkerboard(preview_w, preview_h)
    # alpha 合成：半透明像素与棋盘格自然混合，可直接暴露溢色/半透明瑕疵。
    preview.alpha_composite(crop, (PADDING, PADDING))
    return preview, fully_transparent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sprite_preview",
        description="读取 PNG 图集与 JSON 区域配置，生成带棋盘格透明背景的精灵预览并输出摘要报告。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python sprite_preview.py assets/spritesheet.png regions.json out/previews\n"
            "\n"
            "输出：输出目录下每个有效区域生成 <名称>.png 预览，并生成 report.json 摘要报告。"
        ),
    )
    parser.add_argument("atlas", help="图集路径：必须是含 alpha 通道的 PNG 文件")
    parser.add_argument("config", help="区域配置路径：JSON 数组，每项含 name/x/y/width/height")
    parser.add_argument("output_dir", help="预览与报告的输出目录，不存在时自动创建")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    atlas_path = Path(args.atlas)
    config_path = Path(args.config)
    output_dir = Path(args.output_dir)

    # 第一步：校验图集（不通过则整体终止，不产生任何输出）。
    try:
        atlas, atlas_w, atlas_h = load_atlas(atlas_path)
    except ValueError as exc:
        return fail(str(exc))

    # 第二步：校验配置（不通过则整体终止，不产生任何输出）。
    try:
        raw_regions = load_regions(config_path)
    except ValueError as exc:
        return fail(str(exc))

    # 第三步：逐条校验并记录原始顺序。
    validated: list[dict[str, Any]] = []
    report_entries: list[dict[str, Any]] = []
    duplicate_names: set[str] = set()
    seen_names: set[str] = set()

    for index, entry in enumerate(raw_regions):
        base: dict[str, Any] = {"index": index, "name": entry.get("name") if isinstance(entry, dict) else None}
        region, error = validate_entry(entry, atlas_w, atlas_h)
        if error is not None:
            base.update(status=error, reason=explain(error, entry))
            report_entries.append(base)
            continue

        assert region is not None
        if region["name"] in seen_names:
            duplicate_names.add(region["name"])
        seen_names.add(region["name"])
        validated.append(region)
        report_entries.append(None)  # 占位，待同名覆盖判定后回填

    # 第四步：同名区域以后出现者为准，仅最后一个有效区域参与渲染。
    winner_index_by_name: dict[str, int] = {}
    for pos, region in enumerate(validated):
        winner_index_by_name[region["name"]] = pos

    render_list: list[tuple[dict[str, Any], int]] = []
    cursor = 0
    for report_index, entry in enumerate(report_entries):
        if entry is not None:
            continue  # 该位置已被错误条目占用
        region = validated[cursor]
        pos = cursor
        cursor += 1
        if winner_index_by_name[region["name"]] != pos:
            report_entries[report_index] = {
                "index": report_index,
                "name": region["name"],
                "status": ST_SUPERSEDED,
                "reason": f"同名区域重复，本项已被配置中后出现的同名区域覆盖",
                "duplicate": True,
            }
        else:
            render_list.append((region, report_index))

    # 第五步：准备输出目录（此时尚未写入任何预览）。
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return fail(f"输出目录无法创建：{output_dir}（{exc}）")

    # 第六步：清理上一次运行残留的预览，保证重跑后文件集合与本次有效区域一致。
    previous_report = output_dir / REPORT_NAME
    stale: set[str] = set()
    if previous_report.exists():
        try:
            old = json.loads(previous_report.read_text(encoding="utf-8"))
            for item in old.get("regions", []):
                fname = item.get("preview_file")
                if fname:
                    stale.add(fname)
        except (OSError, json.JSONDecodeError):
            stale = set()

    rendered_files: set[str] = set()
    success_count = 0
    transparent_count = 0
    failed_count = 0

    for region, report_index in render_list:
        entry_report: dict[str, Any] = {
            "index": report_index,
            "name": region["name"],
            "region": {k: region[k] for k in ("x", "y", "width", "height")},
            "duplicate": region["name"] in duplicate_names,
        }
        try:
            preview, fully_transparent = render_preview(atlas, region)
            filename = sanitize_filename(region["name"])
            if filename is None:
                raise OSError("区域名称无法转换为合法文件名")
            target = output_dir / filename
            # 最终防线：解析后必须仍位于输出目录内。
            if output_dir.resolve() not in target.resolve().parents and target.resolve().parent != output_dir.resolve():
                raise OSError("预览路径越出输出目录")
            preview.save(target, format="PNG")
        except OSError as exc:
            entry_report.update(status=ST_WRITE_FAILED, reason=f"预览写入失败：{exc}")
            report_entries[report_index] = entry_report
            failed_count += 1
            continue

        rendered_files.add(filename)
        entry_report.update(
            status=ST_FULLY_TRANSPARENT if fully_transparent else ST_SUCCESS,
            reason="裁剪区域 alpha 全部为 0，预览仅含棋盘格背景" if fully_transparent else "预览生成成功",
            preview_file=filename,
        )
        report_entries[report_index] = entry_report
        if fully_transparent:
            transparent_count += 1
        else:
            success_count += 1

    for fname in stale - rendered_files:
        stale_path = output_dir / fname
        try:
            stale_path.unlink()
        except OSError:
            pass  # 清理失败不影响本次结果

    # 第七步：写摘要报告（图集文件自始至终只读，不会被改动）。
    report = {
        "atlas": {
            "path": str(atlas_path),
            "width": atlas_w,
            "height": atlas_h,
        },
        "config": {"path": str(config_path), "total_entries": len(raw_regions)},
        "output_dir": str(output_dir),
        "duplicate_names": sorted(duplicate_names),
        "summary": {
            "total": len(raw_regions),
            "success": success_count,
            "fully_transparent": transparent_count,
            "write_failed": failed_count,
            "config_invalid": sum(e["status"] == ST_CONFIG_INVALID for e in report_entries),
            "invalid_coordinates": sum(e["status"] == ST_INVALID_COORDS for e in report_entries),
            "out_of_bounds": sum(e["status"] == ST_OUT_OF_BOUNDS for e in report_entries),
            "superseded": sum(e["status"] == ST_SUPERSEDED for e in report_entries),
        },
        "regions": report_entries,
    }
    try:
        (output_dir / REPORT_NAME).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        return fail(f"摘要报告写入失败：{output_dir / REPORT_NAME}（{exc}）")

    print(f"处理完成：共 {len(raw_regions)} 条配置，成功 {success_count}，"
          f"全透明 {transparent_count}，写入失败 {failed_count}，"
          f"其他跳过 {len(raw_regions) - success_count - transparent_count - failed_count}。")
    print(f"摘要报告：{output_dir / REPORT_NAME}")
    return 0


def explain(status: str, entry: Any) -> str:
    if status == ST_CONFIG_INVALID:
        if not isinstance(entry, dict):
            return "配置项必须为 JSON 对象"
        missing = [f for f in REQUIRED_FIELDS if f not in entry]
        if missing:
            return f"缺少必需字段：{', '.join(missing)}"
        if not isinstance(entry.get("name"), str) or not str(entry.get("name", "")).strip():
            return "name 必须为非空字符串"
        return "x/y/width/height 必须为整数（不接受字符串、小数或布尔值）"
    if status == ST_INVALID_COORDS:
        return "坐标必须为非负整数且宽高为正整数，不做裁剪修正"
    if status == ST_OUT_OF_BOUNDS:
        return "区域超出图集边界，不做裁剪修正"
    return status


if __name__ == "__main__":
    sys.exit(main())
