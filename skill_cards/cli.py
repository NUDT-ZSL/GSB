"""命令行入口（需求 9、10、11）。

用法：
    python -m skill_cards --manifest 清单.json --out 输出目录
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

from .pipeline import run_pipeline
from .render import render_card
from .summary import SUMMARY_FILENAME, write_summary

# 本工具在输出目录中拥有的文件名前缀，重复运行时只清理自己的产物
CARD_GLOB = "[0-9][0-9]_*.png"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="skill-cards",
        description="读取技能清单 JSON，逐技能生成音效落点检视卡 PNG 与汇总。",
    )
    parser.add_argument("--manifest", required=True, help="技能清单 JSON 文件路径")
    parser.add_argument("--out", required=True, help="检视卡与汇总的输出目录")
    return parser.parse_args(argv)


def _force_utf8_console() -> None:
    """Windows 管道下默认按 cp936 输出会乱码，统一强制 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_console()
    args = _parse_args(argv)
    manifest_path: str = args.manifest
    out_dir: str = args.out

    # ---- 清单文件必须存在（需求 10）；提示中包含所给路径 ----
    if not os.path.isfile(manifest_path):
        print(f"错误：清单文件不存在：{manifest_path}", file=sys.stderr)
        return 2

    # ---- 读取与 JSON 解析（需求 9）；utf-8-sig 兼容 Windows 编辑器写入的 BOM ----
    try:
        with open(manifest_path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        print(
            f"错误：清单不是可解析的 JSON：{manifest_path}（{exc.msg}，"
            f"第 {exc.lineno} 行第 {exc.colno} 列）",
            file=sys.stderr,
        )
        return 2
    except OSError as exc:
        print(f"错误：无法读取清单 {manifest_path}：{exc}", file=sys.stderr)
        return 2

    # ---- 顶层必须是数组（需求 9），整体拒绝，不产生任何输出 ----
    if not isinstance(data, list):
        print(
            "错误：清单顶层必须是 JSON 数组（[ ... ]），"
            f"实际为 {type(data).__name__}；已整体拒绝，未生成任何检视卡与汇总。",
            file=sys.stderr,
        )
        return 2

    # 全部校验通过后才创建输出目录，保证拒绝场景无新文件
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as exc:
        print(f"错误：无法创建输出目录 {out_dir}：{exc}", file=sys.stderr)
        return 2

    result = run_pipeline(data)

    # ---- 重复运行：清理上次本工具产物并完整重写汇总（需求 11）----
    for stale in glob.glob(os.path.join(out_dir, CARD_GLOB)):
        os.remove(stale)
    stale_summary = os.path.join(out_dir, SUMMARY_FILENAME)
    if os.path.exists(stale_summary):
        os.remove(stale_summary)

    for skill in result.completed:
        render_card(skill, os.path.join(out_dir, skill.filename))

    summary_path = write_summary(result, out_dir)

    print(
        f"完成：检视卡 {len(result.completed)} 张，跳过 {len(result.skipped)} 个，"
        f"未完成 {len(result.unfinished)} 个，越界音效 {result.total_out_of_bounds} 个"
    )
    print(f"输出目录：{os.path.abspath(out_dir)}")
    print(f"汇总文件：{summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
