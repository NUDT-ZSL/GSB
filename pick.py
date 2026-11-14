#!/usr/bin/env python3
"""矢量场景命中拾取命令行入口。

用法（Windows PowerShell，在项目根目录执行）：
    python -I pick.py --scene samples/scene_sample.json --output out/results.json

退出码约定：
    0  正常完成（场景中存在个别非法条目不算失败，错误写入结果 JSON）
    2  场景文件不存在或不可读
    3  场景文件不是合法 JSON
    4  场景整体结构不合法（如缺少 nodes/clicks）
    5  结果文件无法写入
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Windows 控制台默认编码可能是 GBK，统一改为 UTF-8，避免中文提示乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# 以脚本方式运行时把项目根目录加入模块搜索路径，兼容 `python -I pick.py`
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scene_picker.picker import SceneError, pick  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="读取矢量场景 JSON，执行命中拾取并把结果写入 JSON 文件。",
    )
    parser.add_argument("--scene", required=True, help="场景 JSON 文件路径")
    parser.add_argument("--output", required=True, help="拾取结果 JSON 输出路径")
    parser.add_argument("--quiet", action="store_true", help="只输出错误与摘要计数")
    return parser


def main(argv: list | None = None) -> int:
    args = build_parser().parse_args(argv)
    scene_path = Path(args.scene)

    if not scene_path.is_file():
        print(f"错误：场景文件不存在或不是文件: {scene_path}", file=sys.stderr)
        return 2

    try:
        with scene_path.open("r", encoding="utf-8") as handle:
            scene = json.load(handle)
    except json.JSONDecodeError as exc:
        print(f"错误：场景文件不是合法 JSON（{exc.msg}，第 {exc.lineno} 行第 {exc.colno} 列）",
              file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"错误：无法读取场景文件: {exc}", file=sys.stderr)
        return 2

    try:
        result = pick(scene)
    except SceneError as exc:
        print(f"错误：场景结构不合法: {exc}", file=sys.stderr)
        return 4

    output_path = Path(args.output)
    try:
        if output_path.parent and not output_path.parent.exists():
            output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except OSError as exc:
        print(f"错误：无法写入结果文件 {output_path}: {exc}", file=sys.stderr)
        return 5

    summary = result["summary"]
    print(f"拾取完成: 图元 {summary['elements_parsed']} 个"
          f"（可用 {summary['elements_available']}），"
          f"点击 {summary['clicks_total']} 次，"
          f"命中 {summary['hits']}，未命中 {summary['misses']}，"
          f"非法条目 {summary['errors']}，警告 {summary['warnings']}")
    print(f"结果已写入: {output_path}")

    if not args.quiet:
        for item in result["results"]:
            click = item.get("click") or {}
            click_desc = f"({click.get('x')}, {click.get('y')})"
            if "error" in item:
                detail = f"输入错误: {item['error']}"
            elif item["hit"]:
                local = item["local"]
                detail = (f"命中 {item['id']}，"
                          f"局部坐标=({local['x']:.2f}, {local['y']:.2f})")
            else:
                detail = "未命中，局部坐标=null"
            print(f"  [{item['index']}] 点击{click_desc} -> {detail}")
        for item in result["errors"]:
            print(f"  [非法条目] {item['entry']}: {item['reason']}")
        for item in result["warnings"]:
            print(f"  [警告] {item['entry']}: {item['reason']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
