# -*- coding: utf-8 -*-
"""角色部位灰度遮罩 -> RGBA 图集打包工具。

通道约定：R=金属度(metallic)、G=粗糙度(roughness)、B=环境光遮蔽(AO)、A=不透明度(opacity)。
未指定来源的通道：R/G/B 填 0，A 填 255。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from PIL import Image, ImageDraw, UnidentifiedImageError

CHANNELS = ("R", "G", "B", "A")
CHANNEL_INDEX = {"R": 0, "G": 1, "B": 2, "A": 3}
CHANNEL_DEFAULT = {"R": 0, "G": 0, "B": 0, "A": 255}
PREVIEW_SCALE = 3
GRID_LINE_COLOR = (255, 0, 0, 128)  # 半透明红色


class ConfigError(Exception):
    """配置无效（缺字段、类型错误等），消息中必须含具体字段名。"""


# --------------------------------------------------------------------------- #
# 配置解析
# --------------------------------------------------------------------------- #
def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _require(obj: Any, key: str, ctx: str) -> Any:
    if not isinstance(obj, dict) or key not in obj:
        raise ConfigError(f"缺少必填字段 '{ctx}{key}'")
    return obj[key]


def _check_int_field(obj: dict, key: str, ctx: str, positive: bool = True) -> int:
    v = _require(obj, key, ctx)
    if not _is_int(v) or (positive and v <= 0):
        raise ConfigError(f"字段 '{ctx}{key}' 必须为{'正' if positive else ''}整数，实际值: {v!r}")
    return v


def load_config(config_path: str) -> dict:
    """读取并校验打包配置 JSON。无效时抛出 ConfigError，消息含字段名。"""
    if not os.path.isfile(config_path):
        raise ConfigError(f"配置文件不存在: {config_path}")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigError(f"配置 JSON 解析失败: {e}") from e

    if not isinstance(cfg, dict):
        raise ConfigError("配置根节点必须是 JSON 对象")

    for key in ("atlas", "grid", "parts", "output"):
        _require(cfg, key, "")

    atlas = _require(cfg, "atlas", "")
    width = _check_int_field(atlas, "width", "atlas.")
    height = _check_int_field(atlas, "height", "atlas.")

    grid = _require(cfg, "grid", "")
    cols = _check_int_field(grid, "cols", "grid.")
    rows = _check_int_field(grid, "rows", "grid.")

    if width % cols != 0:
        raise ConfigError(f"字段 'atlas.width'({width}) 必须能被 'grid.cols'({cols}) 整除")
    if height % rows != 0:
        raise ConfigError(f"字段 'atlas.height'({height}) 必须能被 'grid.rows'({rows}) 整除")

    output = _require(cfg, "output", "")
    for key in ("atlas_path", "report_path"):
        v = _require(output, key, "output.")
        if not isinstance(v, str) or not v.strip():
            raise ConfigError(f"字段 'output.{key}' 必须为非空字符串")

    parts = _require(cfg, "parts", "")
    if not isinstance(parts, list):
        raise ConfigError("字段 'parts' 必须为数组")
    for i, part in enumerate(parts):
        ctx = f"parts[{i}]."
        if not isinstance(part, dict):
            raise ConfigError(f"字段 'parts[{i}]' 必须为对象")
        for key in ("id", "source", "cell", "anchor", "channel"):
            _require(part, key, ctx)
        if not isinstance(part["id"], str) or not part["id"].strip():
            raise ConfigError(f"字段 '{ctx}id' 必须为非空字符串")
        if not isinstance(part["source"], str) or not part["source"].strip():
            raise ConfigError(f"字段 '{ctx}source' 必须为非空字符串")
        for key in ("cell", "anchor"):
            v = part[key]
            if not (isinstance(v, list) and len(v) == 2 and all(_is_int(x) for x in v)):
                raise ConfigError(f"字段 '{ctx}{key}' 必须为含两个整数的数组，实际值: {v!r}")
        if part["channel"] not in CHANNELS:
            raise ConfigError(
                f"字段 '{ctx}channel' 必须为 R/G/B/A 之一，实际值: {part['channel']!r}"
            )

    if "preview" in cfg and cfg["preview"] is not None:
        preview = cfg["preview"]
        if not isinstance(preview, dict):
            raise ConfigError("字段 'preview' 必须为对象")
        for key in ("x", "y", "width", "height"):
            _check_int_field(preview, key, "preview.")
        v = _require(preview, "output_path", "preview.")
        if not isinstance(v, str) or not v.strip():
            raise ConfigError("字段 'preview.output_path' 必须为非空字符串")

    cfg["_base_dir"] = os.path.dirname(os.path.abspath(config_path))
    return cfg


def resolve_path(base_dir: str, p: str) -> str:
    return p if os.path.isabs(p) else os.path.abspath(os.path.join(base_dir, p))


# --------------------------------------------------------------------------- #
# 图片载入：灰度 / 灰度+透明 / RGB -> 单通道灰度 L
# --------------------------------------------------------------------------- #
def load_gray_png(path: str) -> Image.Image:
    """打开 PNG 并统一转为单通道灰度（模式 L）。

    损坏、截断或无法解码时抛出 OSError（含 UnidentifiedImageError）。
    """
    try:
        img = Image.open(path)
        img.load()  # 强制解码，截断 PNG 在此处抛错
    except (OSError, UnidentifiedImageError) as e:
        raise OSError(f"图片无法打开或解码失败: {type(e).__name__}: {e}") from e
    return img.convert("L")


# --------------------------------------------------------------------------- #
# 打包
# --------------------------------------------------------------------------- #
@dataclass
class PartRecord:
    id: str
    source: str
    channel: str
    cell: list
    anchor: list
    status: str = ""  # success / skipped
    reason: str | None = None
    source_size: list | None = None
    target_region: list | None = None  # 实际写入图集的矩形 [x0,y0,x1,y1]
    clipped: bool = False


@dataclass
class PackResult:
    ok: bool
    config: dict
    report: dict = field(default_factory=dict)
    atlas: Image.Image | None = None
    atlas_path: str | None = None
    report_path: str | None = None
    preview_path: str | None = None


def run_pack(cfg: dict) -> PackResult:
    base_dir = cfg["_base_dir"]
    width, height = cfg["atlas"]["width"], cfg["atlas"]["height"]
    cols, rows = cfg["grid"]["cols"], cfg["grid"]["rows"]
    cell_w, cell_h = width // cols, height // rows

    # 四个通道带，A 默认 255，其余默认 0
    bands = {ch: Image.new("L", (width, height), CHANNEL_DEFAULT[ch]) for ch in CHANNELS}
    records: list[PartRecord] = []
    errors: list[dict] = []

    for part in cfg["parts"]:
        pid = part["id"]
        src_rel = part["source"]
        src_path = resolve_path(base_dir, src_rel)
        ch = part["channel"]
        col, row = part["cell"]
        ax, ay = part["anchor"]
        rec = PartRecord(id=pid, source=src_rel, channel=ch,
                         cell=[col, row], anchor=[ax, ay])

        # 1) 网格位置越界 -> 跳过
        if not (0 <= col < cols and 0 <= row < rows):
            rec.status = "skipped"
            rec.reason = f"网格位置越界: cell=[{col},{row}]，合法范围 col∈[0,{cols - 1}], row∈[0,{rows - 1}]"
            records.append(rec)
            errors.append({"id": pid, "source": src_rel, "reason": rec.reason})
            continue

        # 2) 载入图片（损坏/缺失 -> 跳过，不影响其他部位）
        try:
            gray = load_gray_png(src_path)
        except (OSError, ValueError) as e:
            rec.status = "skipped"
            rec.reason = f"{os.path.basename(src_rel)}: {e}"
            records.append(rec)
            errors.append({"id": pid, "source": src_rel, "reason": rec.reason})
            continue

        rec.source_size = [gray.width, gray.height]

        # 3) 锚点越界（相对来源图）-> 跳过
        if not (0 <= ax < gray.width and 0 <= ay < gray.height):
            rec.status = "skipped"
            rec.reason = (
                f"锚点越界: anchor=[{ax},{ay}]，来源图尺寸 {gray.width}x{gray.height}，"
                f"合法范围 x∈[0,{gray.width - 1}], y∈[0,{gray.height - 1}]"
            )
            records.append(rec)
            errors.append({"id": pid, "source": src_rel, "reason": rec.reason})
            continue

        # 4) 锚点对齐到网格单元左上角，计算粘贴位置并按图集边界裁剪
        cell_x, cell_y = col * cell_w, row * cell_h
        dx, dy = cell_x - ax, cell_y - ay
        sx0 = max(0, -dx)
        sy0 = max(0, -dy)
        sx1 = min(gray.width, width - dx)
        sy1 = min(gray.height, height - dy)
        if sx1 <= sx0 or sy1 <= sy0:
            rec.status = "skipped"
            rec.reason = "放置区域完全落在图集范围外"
            records.append(rec)
            errors.append({"id": pid, "source": src_rel, "reason": rec.reason})
            continue

        crop = gray.crop((sx0, sy0, sx1, sy1))
        paste_pos = (dx + sx0, dy + sy0)
        bands[ch].paste(crop, paste_pos)
        rec.status = "success"
        rec.target_region = [paste_pos[0], paste_pos[1],
                             paste_pos[0] + crop.width, paste_pos[1] + crop.height]
        rec.clipped = (sx0, sy0, sx1, sy1) != (0, 0, gray.width, gray.height)
        records.append(rec)

    success = [r for r in records if r.status == "success"]
    skipped = [r for r in records if r.status == "skipped"]

    # 通道映射摘要
    channel_mapping = {}
    for ch in CHANNELS:
        channel_mapping[ch] = {
            "meaning": {"R": "metallic", "G": "roughness",
                        "B": "ambient_occlusion", "A": "opacity"}[ch],
            "default_fill": CHANNEL_DEFAULT[ch],
            "assigned_parts": [r.id for r in records if r.channel == ch],
            "successful_parts": [r.id for r in success if r.channel == ch],
        }

    result = PackResult(ok=bool(success), config=cfg)

    # 全部部位不可用 -> 不生成图集
    if not success:
        result.report = _base_report(cfg, records, errors, channel_mapping, bands=None)
        result.report["status"] = "failure"
        result.report["failure_reason"] = "所有部位均不可用，未生成图集"
        result.report_path = _write_report(cfg, result.report)
        _remove_existing(cfg, "atlas_path")
        return result

    atlas = Image.merge("RGBA", [bands["R"], bands["G"], bands["B"], bands["A"]])
    result.atlas = atlas

    # 通道统计：非零像素占比
    total_px = width * height
    channel_stats = {}
    for ch in CHANNELS:
        hist = bands[ch].histogram()
        nonzero = sum(hist[1:])
        channel_stats[ch] = round(nonzero / total_px, 6)

    report = _base_report(cfg, records, errors, channel_mapping, bands=channel_stats)
    result.report = report

    # 预览
    if cfg.get("preview"):
        preview_info = _build_preview(atlas, cfg, cell_w, cell_h)
        report["preview"] = preview_info
        result.preview_path = preview_info["path"]
    else:
        report["preview"] = None

    atlas_path = resolve_path(base_dir, cfg["output"]["atlas_path"])
    os.makedirs(os.path.dirname(atlas_path) or ".", exist_ok=True)
    atlas.save(atlas_path, format="PNG")
    result.atlas_path = atlas_path

    result.report_path = _write_report(cfg, report)
    return result


def _base_report(cfg: dict, records: list[PartRecord], errors: list,
                 channel_mapping: dict, bands) -> dict:
    width, height = cfg["atlas"]["width"], cfg["atlas"]["height"]
    cols, rows = cfg["grid"]["cols"], cfg["grid"]["rows"]
    success = [r for r in records if r.status == "success"]
    skipped = [r for r in records if r.status == "skipped"]
    report = {
        "status": "success",
        "failure_reason": None,
        "atlas": {
            "path": resolve_path(cfg["_base_dir"], cfg["output"]["atlas_path"]),
            "width": width,
            "height": height,
            "mode": "RGBA",
        },
        "grid": {"rows": rows, "cols": cols,
                 "cell_width": width // cols, "cell_height": height // rows},
        "parts": [
            {
                "id": r.id,
                "source": r.source,
                "channel": r.channel,
                "cell": r.cell,
                "anchor": r.anchor,
                "status": r.status,
                "reason": r.reason,
                "source_size": r.source_size,
                "target_region": r.target_region,
                "clipped": r.clipped,
            }
            for r in records
        ],
        "summary": {
            "total": len(records),
            "success": len(success),
            "skipped": len(skipped),
        },
        "successful_parts": [r.id for r in success],
        "skipped_parts": [r.id for r in skipped],
        "channel_mapping": channel_mapping,
        "errors": errors,
    }
    if bands is not None:
        report["channel_stats"] = bands
    return report


def _write_report(cfg: dict, report: dict) -> str:
    path = resolve_path(cfg["_base_dir"], cfg["output"]["report_path"])
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return path


def _remove_existing(cfg: dict, key: str) -> None:
    path = resolve_path(cfg["_base_dir"], cfg["output"][key])
    if os.path.exists(path):
        os.remove(path)


def _build_preview(atlas: Image.Image, cfg: dict, cell_w: int, cell_h: int) -> dict:
    p = cfg["preview"]
    rx, ry, rw, rh = p["x"], p["y"], p["width"], p["height"]

    x0, y0 = max(0, rx), max(0, ry)
    x1, y1 = min(atlas.width, rx + rw), min(atlas.height, ry + rh)
    clipped = (x0, y0, x1, y1) != (rx, ry, rx + rw, ry + rh)
    note = "请求区域越界，已裁剪到图集范围" if clipped else None

    region = atlas.crop((x0, y0, x1, y1))
    big = region.resize(
        (region.width * PREVIEW_SCALE, region.height * PREVIEW_SCALE),
        Image.Resampling.NEAREST,
    )
    draw = ImageDraw.Draw(big)
    # 与打包网格间距一致的 1px 半透明红网格线
    for k in range(1, cfg["grid"]["cols"]):
        gx = k * cell_w
        px = (gx - x0) * PREVIEW_SCALE
        if 0 <= px < big.width:
            draw.line([(px, 0), (px, big.height - 1)], fill=GRID_LINE_COLOR)
    for k in range(1, cfg["grid"]["rows"]):
        gy = k * cell_h
        py = (gy - y0) * PREVIEW_SCALE
        if 0 <= py < big.height:
            draw.line([(0, py), (big.width - 1, py)], fill=GRID_LINE_COLOR)

    out_path = resolve_path(cfg["_base_dir"], p["output_path"])
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    big.save(out_path, format="PNG")
    return {
        "path": out_path,
        "scale": PREVIEW_SCALE,
        "requested_region": {"x": rx, "y": ry, "width": rw, "height": rh},
        "clipped": clipped,
        "note": note,
        "clipped_region": {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0},
        "size": [big.width, big.height],
        "grid_line_color": list(GRID_LINE_COLOR),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def fallback_report_path(config_path: str) -> str:
    """配置加载失败时，报告写到配置文件同目录。"""
    d = os.path.dirname(os.path.abspath(config_path))
    stem = os.path.splitext(os.path.basename(config_path))[0]
    return os.path.join(d, f"{stem}.fallback_report.json")


def write_failure_report(config_path: str, reason: str) -> str:
    path = fallback_report_path(config_path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = {
        "status": "failure",
        "failure_reason": reason,
        "config_path": os.path.abspath(config_path),
        "parts": [],
        "summary": {"total": 0, "success": 0, "skipped": 0},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("用法: python -m mask_packer.packer <打包配置.json>")
        return 2
    config_path = argv[1]
    try:
        cfg = load_config(config_path)
    except ConfigError as e:
        reason = f"配置加载失败: {e}"
        report_path = write_failure_report(config_path, reason)
        print(f"失败原因: {reason}")
        print(f"报告路径: {report_path}")
        return 1

    result = run_pack(cfg)
    if not result.ok:
        print(f"失败原因: {result.report['failure_reason']}")
        print(f"报告路径: {result.report_path}")
        return 1

    print(f"图集路径: {result.atlas_path}")
    print(f"成功部位数: {result.report['summary']['success']}")
    print(f"跳过部位数: {result.report['summary']['skipped']}")
    print(f"报告路径: {result.report_path}")
    if result.preview_path:
        print(f"预览路径: {result.preview_path}")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))
