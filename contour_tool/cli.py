"""Command-line entry point: scene JSON in, preview PNG + result JSON out."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .contour import extract_all, locate
from .errors import OutputError, SceneError
from .output import save_outputs
from .render import render_preview
from .report import build_result
from .scene import load_scene


def run(
    scene_path: str, png_path: str, json_path: str, draw_markers: bool = False
) -> int:
    loaded = load_scene(scene_path)
    scene = loaded.scene
    contours = extract_all(scene)
    hits = locate(scene, contours)
    markers = [q for q in scene.queries if q is not None] if draw_markers else None
    report = build_result(scene, contours, loaded.affected_levels, loaded.warnings, hits)

    for message in loaded.warnings:
        print(f"警告: {message}", file=sys.stderr)

    save_outputs(
        png_writer=lambda: render_preview(scene, contours, markers),
        json_writer=lambda: report,
        png_path=png_path,
        json_path=json_path,
    )
    print(f"预览图：{Path(png_path)}")
    print(f"定位结果：{Path(json_path)}")
    summary = report["summary"]
    print(
        f"级别 {summary['level_count']} 个（空结果 {summary['empty_level_count']}），"
        f"查询 {summary['query_count']} 个："
        f"命中 {summary['hit_count']}，未命中 {summary['miss_count']}，"
        f"跳过 {summary['skipped_count']}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="contour_tool",
        description="将规则网格标量场渲染为等值线预览 PNG，并定位查询点的最近等值线。",
    )
    parser.add_argument("scene", help="输入场景 JSON 路径")
    parser.add_argument("--png", required=True, help="输出预览 PNG 路径")
    parser.add_argument("--json", required=True, dest="json_path", help="输出定位结果 JSON 路径")
    parser.add_argument(
        "--markers",
        action="store_true",
        help="在预览图上标出查询点位置（默认不标记）",
    )
    args = parser.parse_args(argv)
    try:
        return run(args.scene, args.png, args.json_path, draw_markers=args.markers)
    except SceneError as exc:
        print(f"场景错误：{exc}", file=sys.stderr)
        return 2
    except OutputError as exc:
        print(f"输出错误：{exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
