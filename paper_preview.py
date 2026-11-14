#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""二维纸片模型遮挡暗部离线合成器。

读取本地场景 JSON 和 PNG 贴图，按层级从后到前合成预览；同时按更高层级
网格对每个网格像素的遮挡程度压暗颜色，输出合成预览 PNG 与处理报告 JSON。

只依赖 Pillow，不使用实时渲染器、网络或容器。
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops

DEFAULT_OCCLUSION_STRENGTH = 0.65
MAX_CANVAS_PIXELS = 16_777_216
REQUIRED_SCENE_FIELDS = ("canvas", "meshes")
REQUIRED_CANVAS_FIELDS = ("width", "height", "background")
REQUIRED_MESH_FIELDS = ("x", "y", "layer", "texture")
_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


class SceneRejected(Exception):
    """整个场景无法处理时抛出，消息会写入报告中的可读原因。"""


@dataclass
class MeshPlan:
    """单个网格从校验到合成的中间状态。"""

    index: int
    name: str
    layer: int | None = None
    texture: str | None = None
    status: str = "skipped"  # pending / composited / skipped
    reason: str | None = None
    partially_clipped: bool = False
    x: int | None = None
    y: int | None = None
    texture_size: tuple[int, int] | None = None
    full_rect: tuple[int, int, int, int] | None = None
    placed_rect: tuple[int, int, int, int] | None = None
    source_rect: tuple[int, int, int, int] | None = None
    image: Image.Image | None = field(default=None, repr=False)
    occlusion: Image.Image | None = field(default=None, repr=False)
    shaded: Image.Image | None = field(default=None, repr=False)
    opaque_pixels: int = 0
    occluded_pixels: int = 0
    occluded_pixel_ratio: float = 0.0
    mean_occlusion: float = 0.0


# ---------------------------------------------------------------------------
# 基础校验工具
# ---------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _as_integral_number(value: Any) -> int | None:
    """接受整数或整数值浮点数；其余（含布尔、字符串、NaN/Inf）返回 None。"""
    if not _is_number(value) or not math.isfinite(value):
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
    return int(value)


def _as_pixel(value: Any) -> int | None:
    """坐标允许任意有限数值，四舍五入到最近像素。"""
    if not _is_number(value) or not math.isfinite(value):
        return None
    return int(round(value))


def _describe(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(value)
    if len(text) > 48:
        text = text[:45] + "..."
    return text


def _scene_name(scene_path: Path) -> str:
    return scene_path.name or str(scene_path)


# ---------------------------------------------------------------------------
# 场景 / 画布校验
# ---------------------------------------------------------------------------


def _load_scene_json(scene_path: Path) -> dict[str, Any]:
    try:
        with open(scene_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        raise SceneRejected(f"场景文件不存在: {_scene_name(scene_path)}") from None
    except OSError:
        raise SceneRejected(f"场景文件不可读: {_scene_name(scene_path)}") from None
    except json.JSONDecodeError as exc:
        raise SceneRejected(
            f"场景JSON解析失败: 第 {exc.lineno} 行第 {exc.colno} 列, {exc.msg}"
        ) from None
    if not isinstance(data, dict):
        raise SceneRejected("场景JSON顶层必须为对象")
    missing = [name for name in REQUIRED_SCENE_FIELDS if name not in data]
    if missing:
        raise SceneRejected("场景缺少必需字段: " + ", ".join(missing))
    return data


def _check_dimension(label: str, value: Any) -> tuple[int | None, str | None]:
    if not _is_number(value) or not math.isfinite(value):
        return None, f"画布{label}非法：必须为正整数（收到 {_describe(value)}）"
    if isinstance(value, float) and not value.is_integer():
        return None, f"画布{label}非法：必须为整数（收到 {_describe(value)}）"
    number = int(value)
    if number <= 0:
        return None, f"画布{label}非法：必须大于 0（收到 {_describe(value)}）"
    return number, None


def _parse_background(value: Any) -> tuple[int, int, int]:
    if isinstance(value, str):
        if not _HEX_RE.match(value):
            raise SceneRejected(
                f"画布背景色非法：必须为 #rgb 或 #rrggbb（收到 {_describe(value)}）"
            )
        body = value[1:]
        if len(body) == 3:
            body = "".join(ch * 2 for ch in body)
        return int(body[0:2], 16), int(body[2:4], 16), int(body[4:6], 16)
    if isinstance(value, (list, tuple)) and len(value) in (3, 4):
        channels: list[int] = []
        for item in value[:3]:
            channel = _as_integral_number(item)
            if channel is None or not 0 <= channel <= 255:
                raise SceneRejected(
                    "画布背景色非法：RGB 分量必须为 0-255 的整数"
                    f"（收到 {_describe(value)}）"
                )
            channels.append(channel)
        return channels[0], channels[1], channels[2]
    raise SceneRejected(
        "画布背景色非法：必须为 [r, g, b]、[r, g, b, a] 或 #rrggbb"
        f"（收到 {_describe(value)}）"
    )


def _parse_occlusion_strength(scene: dict[str, Any]) -> float:
    if "occlusion_strength" not in scene:
        return DEFAULT_OCCLUSION_STRENGTH
    value = scene["occlusion_strength"]
    if not _is_number(value) or not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise SceneRejected(
            "字段 occlusion_strength 非法：必须为 0 到 1 之间的数值"
            f"（收到 {_describe(value)}）"
        )
    return float(value)


def _validate_canvas(scene: dict[str, Any]) -> tuple[int, int, tuple[int, int, int]]:
    canvas = scene.get("canvas")
    if not isinstance(canvas, dict):
        raise SceneRejected("字段 canvas 非法：必须为对象")
    missing = [name for name in REQUIRED_CANVAS_FIELDS if name not in canvas]
    if missing:
        raise SceneRejected("画布缺少必需字段: " + ", ".join(missing))

    issues: list[str] = []
    width, issue = _check_dimension("宽度", canvas["width"])
    if issue:
        issues.append(issue)
    height, issue = _check_dimension("高度", canvas["height"])
    if issue:
        issues.append(issue)
    try:
        background = _parse_background(canvas["background"])
    except SceneRejected as exc:
        issues.append(str(exc))
        background = (0, 0, 0)
    if issues:
        raise SceneRejected("；".join(issues))
    assert width is not None and height is not None
    if width * height > MAX_CANVAS_PIXELS:
        raise SceneRejected(
            f"画布尺寸过大：{width}x{height} 超过上限 {MAX_CANVAS_PIXELS} 像素"
        )
    return width, height, background


def _validate_meshes_field(scene: dict[str, Any]) -> list[Any]:
    meshes = scene.get("meshes")
    if not isinstance(meshes, list):
        raise SceneRejected("字段 meshes 非法：必须为数组")
    return meshes


# ---------------------------------------------------------------------------
# 单个网格校验
# ---------------------------------------------------------------------------


def _plan_mesh(index: int, raw: Any) -> MeshPlan:
    name = f"mesh_{index}"
    if isinstance(raw, dict):
        candidate = raw.get("name")
        if isinstance(candidate, str) and candidate.strip():
            name = candidate.strip()
    plan = MeshPlan(index=index, name=name)
    if not isinstance(raw, dict):
        plan.reason = "网格条目不是JSON对象"
        return plan

    raw_texture = raw.get("texture")
    if isinstance(raw_texture, str):
        plan.texture = raw_texture

    missing = [field_name for field_name in REQUIRED_MESH_FIELDS if field_name not in raw]
    if missing:
        plan.layer = _as_integral_number(raw.get("layer"))
        plan.x = _as_pixel(raw.get("x"))
        plan.y = _as_pixel(raw.get("y"))
        plan.reason = "缺少必需字段: " + ", ".join(missing)
        return plan

    issues: list[str] = []

    layer = _as_integral_number(raw["layer"])
    if layer is None:
        issues.append(
            "层级顺序 layer 非法：必须为整数"
            f"（收到 {_describe(raw['layer'])}）"
        )

    x = _as_pixel(raw["x"])
    if x is None:
        issues.append(f"坐标 x 非法：必须为有限数值（收到 {_describe(raw['x'])}）")

    y = _as_pixel(raw["y"])
    if y is None:
        issues.append(f"坐标 y 非法：必须为有限数值（收到 {_describe(raw['y'])}）")

    texture = raw["texture"]
    if not isinstance(texture, str) or not texture.strip():
        issues.append(
            f"贴图文件名 texture 非法：必须为非空字符串（收到 {_describe(texture)}）"
        )
        plan.texture = None
    else:
        plan.texture = texture

    plan.layer = layer
    plan.x = x
    plan.y = y
    if issues:
        plan.reason = "字段非法：" + "；".join(issues)
    else:
        plan.status = "pending"
    return plan


def _ordered(plans: list[MeshPlan]) -> list[MeshPlan]:
    """层级升序（后到前）；层级缺失/非法的条目排在最后，并保持原始顺序。"""
    return sorted(
        plans,
        key=lambda item: (
            (0, item.layer, item.index)
            if item.layer is not None
            else (1, 0, item.index)
        ),
    )


# ---------------------------------------------------------------------------
# 合成与遮挡
# ---------------------------------------------------------------------------


def _factor_lut(strength: float) -> list[int]:
    """遮挡值 0-255 → 亮度系数 0-255；0 遮挡时严格为 255（保持原色）。"""
    return [
        max(0, min(255, int(round(255.0 * (1.0 - strength * value / 255.0)))))
        for value in range(256)
    ]


def _shade(image: Image.Image, occlusion: Image.Image, lut: list[int]) -> Image.Image:
    factor = occlusion.point(lut)
    factor_rgb = Image.merge("RGB", (factor, factor, factor))
    red, green, blue = ImageChops.multiply(image.convert("RGB"), factor_rgb).split()
    return Image.merge("RGBA", (red, green, blue, image.getchannel("A")))


def _occlusion_stats(
    occlusion: Image.Image, alpha: Image.Image
) -> tuple[int, int, float, float]:
    opaque = sum(alpha.histogram()[1:])
    if opaque == 0:
        return 0, 0, 0.0, 0.0
    opaque_mask = alpha.point(lambda value: 255 if value > 0 else 0)
    relevant = ImageChops.multiply(occlusion, opaque_mask)
    histogram = relevant.histogram()
    occluded = sum(histogram[1:])
    total = sum(value * count for value, count in enumerate(histogram) if value)
    ratio = round(occluded / opaque, 4)
    mean = round(total / (255.0 * opaque), 4)
    return opaque, occluded, ratio, mean


def _place_texture(
    plan: MeshPlan, canvas_size: tuple[int, int], scene_dir: Path
) -> None:
    """校验贴图路径并生成画布大小的 RGBA 图层；失败时写 skip 原因。"""
    texture_name = plan.texture or ""
    texture_path = Path(texture_name)
    if not texture_path.is_absolute():
        texture_path = scene_dir / texture_path
    try:
        resolved = texture_path.resolve()
    except OSError:
        plan.reason = f"贴图路径无法解析: {texture_name}"
        plan.status = "skipped"
        return

    if not resolved.is_file():
        plan.reason = f"贴图文件不存在: {texture_name}"
        plan.status = "skipped"
        return

    try:
        with Image.open(resolved) as opened:
            opened.load()
            texture = opened.convert("RGBA")
    except Exception as exc:  # 单张损坏贴图不应中断整份场景的合成
        plan.reason = f"贴图文件无法读取: {texture_name}（{type(exc).__name__}）"
        plan.status = "skipped"
        return

    canvas_width, canvas_height = canvas_size
    width, height = texture.size
    assert plan.x is not None and plan.y is not None
    x, y = plan.x, plan.y
    left = max(0, x)
    top = max(0, y)
    right = min(canvas_width, x + width)
    bottom = min(canvas_height, y + height)
    if right <= left or bottom <= top:
        plan.reason = "网格完全位于画布外（绘制区域与画布无交集）"
        plan.status = "skipped"
        return

    layer = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    layer.paste(texture.crop((left - x, top - y, right - x, bottom - y)), (left, top))
    plan.image = layer
    plan.texture_size = (width, height)
    plan.full_rect = (x, y, x + width, y + height)
    plan.placed_rect = (left, top, right, bottom)
    plan.source_rect = (left - x, top - y, right - x, bottom - y)
    plan.partially_clipped = (left, top, right, bottom) != (x, y, x + width, y + height)
    plan.status = "composited"


def _compose(
    plans: list[MeshPlan],
    canvas_size: tuple[int, int],
    background: tuple[int, int, int],
    strength: float,
) -> Image.Image:
    composited = [plan for plan in plans if plan.status == "composited"]

    # 从最前层向后累积“透射率”，同一层之间按需求互不遮挡。
    groups: dict[int, list[MeshPlan]] = {}
    for plan in composited:
        assert plan.layer is not None
        groups.setdefault(plan.layer, []).append(plan)

    transmission = Image.new("L", canvas_size, 255)
    for layer_value in sorted(groups, reverse=True):
        group = groups[layer_value]
        for plan in group:
            plan.occlusion = ImageChops.invert(transmission)
        for plan in group:
            assert plan.image is not None
            alpha = plan.image.getchannel("A")
            transmission = ImageChops.multiply(transmission, ImageChops.invert(alpha))

    lut = _factor_lut(strength)
    canvas = Image.new("RGBA", canvas_size, background + (255,))
    for plan in composited:
        assert plan.image is not None and plan.occlusion is not None
        alpha = plan.image.getchannel("A")
        (
            plan.opaque_pixels,
            plan.occluded_pixels,
            plan.occluded_pixel_ratio,
            plan.mean_occlusion,
        ) = _occlusion_stats(plan.occlusion, alpha)
        plan.shaded = _shade(plan.image, plan.occlusion, lut)
        canvas = Image.alpha_composite(canvas, plan.shaded)

    return canvas.convert("RGB")


# ---------------------------------------------------------------------------
# 报告与输出
# ---------------------------------------------------------------------------


def _entry_dict(plan: MeshPlan, order: int) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "order": order,
        "index": plan.index,
        "name": plan.name,
        "layer": plan.layer,
        "texture": plan.texture,
        "status": plan.status,
        "partially_clipped": plan.partially_clipped,
    }
    if plan.status == "composited":
        assert plan.texture_size is not None
        assert plan.full_rect is not None
        assert plan.placed_rect is not None
        assert plan.source_rect is not None
        entry["texture_size"] = list(plan.texture_size)
        entry["full_rect"] = list(plan.full_rect)
        entry["placed_rect"] = list(plan.placed_rect)
        entry["source_rect"] = list(plan.source_rect)
        entry["opaque_pixels"] = plan.opaque_pixels
        entry["occluded_pixels"] = plan.occluded_pixels
        entry["occluded_pixel_ratio"] = plan.occluded_pixel_ratio
        entry["mean_occlusion"] = plan.mean_occlusion
    else:
        entry["unprocessed_reason"] = plan.reason or "未处理"
    return entry


def _build_report(
    scene_name: str,
    status: str,
    plans: list[MeshPlan],
    canvas_info: dict[str, Any],
    strength: float | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entries = [_entry_dict(plan, order) for order, plan in enumerate(plans)]
    composited = sum(1 for plan in plans if plan.status == "composited")
    report: dict[str, Any] = {
        "scene": scene_name,
        "status": status,
        "canvas": canvas_info,
    }
    if strength is not None:
        report["occlusion_strength"] = strength
    if extra:
        report.update(extra)
    report["meshes"] = entries
    report["summary"] = {
        "total": len(plans),
        "composited": composited,
        "skipped": len(plans) - composited,
    }
    return report


def _write_report(path: Path, report: dict[str, Any]) -> None:
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_.-]+", "_", name).strip("._")
    return cleaned or "mesh"


def _write_mesh_previews(preview_dir: Path, plans: list[MeshPlan]) -> int:
    preview_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for order, plan in enumerate(plans):
        if plan.status != "composited" or plan.shaded is None:
            continue
        filename = f"{order:03d}_{plan.index:03d}_{_safe_name(plan.name)}.png"
        plan.shaded.save(preview_dir / filename, format="PNG")
        written += 1
    return written


def _reject_scene(
    scene_name: str,
    reason: str,
    raw_scene: Any,
    out_dir: Path,
    preview_path: Path,
) -> int:
    plans: list[MeshPlan] = []
    if isinstance(raw_scene, dict) and isinstance(raw_scene.get("meshes"), list):
        plans = [_plan_mesh(index, raw) for index, raw in enumerate(raw_scene["meshes"])]
        for plan in plans:
            plan.status = "skipped"
            plan.partially_clipped = False
            plan.reason = f"场景被拒绝：{reason}"
        plans = _ordered(plans)

    report = _build_report(
        scene_name=scene_name,
        status="rejected",
        plans=plans,
        canvas_info={"valid": False},
        strength=None,
        extra={"reason": reason},
    )
    # 拒绝整个场景时不产生预览；清理同名的旧预览，避免误读上一次结果。
    try:
        preview_path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        _write_report(out_dir / "report.json", report)
    except OSError as exc:
        print(f"错误: 无法写入报告 {out_dir / 'report.json'}: {exc}", file=sys.stderr)
        return 1

    print(f"场景: {scene_name}")
    print("状态: rejected（场景被拒绝，未产生预览）")
    print(f"原因: {reason}")
    print(f"报告: {(out_dir / 'report.json').resolve()}")
    return 2


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _run(args: argparse.Namespace) -> int:
    scene_path = Path(args.scene)
    out_dir = Path(args.out)
    scene_name = _scene_name(scene_path)
    preview_path = out_dir / "preview.png"
    report_path = out_dir / "report.json"

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"错误: 无法创建输出目录 {out_dir}: {exc}", file=sys.stderr)
        return 1

    raw_scene: Any = None
    try:
        raw_scene = _load_scene_json(scene_path)
        canvas_width, canvas_height, background = _validate_canvas(raw_scene)
        strength = _parse_occlusion_strength(raw_scene)
        raw_meshes = _validate_meshes_field(raw_scene)
    except SceneRejected as exc:
        return _reject_scene(scene_name, str(exc), raw_scene, out_dir, preview_path)

    plans = _ordered([_plan_mesh(index, raw) for index, raw in enumerate(raw_meshes)])
    scene_dir = scene_path.resolve().parent
    canvas_size = (canvas_width, canvas_height)

    for plan in plans:
        if plan.status == "pending":
            _place_texture(plan, canvas_size, scene_dir)

    preview = _compose(plans, canvas_size, background, strength)
    composited_count = sum(1 for plan in plans if plan.status == "composited")
    skipped_count = len(plans) - composited_count
    top_status = "completed" if skipped_count == 0 else "partial"

    canvas_info = {
        "width": canvas_width,
        "height": canvas_height,
        "background": "#{:02x}{:02x}{:02x}".format(*background),
    }
    report = _build_report(
        scene_name=scene_name,
        status=top_status,
        plans=plans,
        canvas_info=canvas_info,
        strength=strength,
    )

    try:
        preview.save(preview_path, format="PNG")
        _write_report(report_path, report)
    except OSError as exc:
        print(f"错误: 写出结果失败: {exc}", file=sys.stderr)
        return 1

    if args.mesh_previews:
        _write_mesh_previews(Path(args.mesh_previews), plans)

    status_text = "completed（完整合成）" if top_status == "completed" else "partial（部分完成）"
    print(f"场景: {scene_name}")
    print(f"状态: {status_text}")
    print(f"画布: {canvas_width}x{canvas_height}")
    print(
        f"网格: 共 {len(plans)}，成功 {composited_count}，跳过 {skipped_count}"
    )
    print(f"预览: {preview_path.resolve()}")
    print(f"报告: {report_path.resolve()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="离线合成二维纸片模型预览，并按更高层级网格的遮挡程度压暗像素。"
    )
    parser.add_argument("--scene", required=True, help="场景 JSON 文件路径")
    parser.add_argument("--out", required=True, help="输出目录（preview.png、report.json）")
    parser.add_argument(
        "--mesh-previews",
        default=None,
        metavar="DIR",
        help="可选：输出每个已合成网格的单独阴影预览（便于核对遮挡效果）",
    )
    return _run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
