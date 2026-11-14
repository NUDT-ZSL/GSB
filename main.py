"""命令行入口:读取三份输入,输出 JSON 报告与代表帧 PNG。

用法:
    python main.py --srt samples/subtitles.srt \
                   --transitions samples/transitions.json \
                   --canvas samples/canvas.json \
                   --out output

任一路径缺省或文件缺失/格式错误时,记录该输入错误并继续处理其余输入:
- SRT 缺失/损坏     -> 无字幕可检,报告照常输出(条目为空);
- 转场清单缺失/损坏 -> 记录错误并跳过转场检测,其余检测照常;
- 画幅配置缺失/损坏 -> 记录错误并回退到内置默认画幅(1920x1080@25fps)。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from subtitle_qc.core import analyze
from subtitle_qc.parsers import Canvas, parse_canvas, parse_srt, parse_transitions


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8-sig") as f:
        return f.read()


def load_inputs(srt_path, transitions_path, canvas_path):
    """逐项加载输入,返回 (subtitles, transitions, canvas, input_errors)。"""
    input_errors: list[dict] = []

    subtitles = []
    try:
        subtitles, srt_errors = parse_srt(_read(srt_path))
        input_errors.extend({"input": "srt", **e} for e in srt_errors)
    except (OSError, UnicodeDecodeError) as e:
        input_errors.append({"input": "srt", "error": f"读取失败: {e}"})

    transitions = []
    transitions_ok = True
    try:
        transitions = parse_transitions(_read(transitions_path))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        transitions_ok = False
        input_errors.append({"input": "transitions", "error": f"解析失败,跳过转场检测: {e}"})

    canvas = Canvas()
    try:
        canvas = parse_canvas(_read(canvas_path))
    except (OSError, UnicodeDecodeError, ValueError, KeyError) as e:
        input_errors.append({"input": "canvas", "error": f"解析失败,使用默认画幅: {e}"})

    return subtitles, transitions, transitions_ok, canvas, input_errors


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="分镜预览片字幕/转场时序质检")
    ap.add_argument("--srt", default=os.path.join("samples", "subtitles.srt"))
    ap.add_argument("--transitions", default=os.path.join("samples", "transitions.json"))
    ap.add_argument("--canvas", default=os.path.join("samples", "canvas.json"))
    ap.add_argument("--out", default="output", help="输出目录(报告与 PNG)")
    args = ap.parse_args(argv)

    subtitles, transitions, transitions_ok, canvas, input_errors = load_inputs(
        args.srt, args.transitions, args.canvas)

    frames_dir = os.path.join(args.out, "frames")
    report = analyze(subtitles, transitions, canvas, frames_dir=frames_dir)
    report["inputs"] = {
        "srt": args.srt,
        "transitions": args.transitions,
        "canvas": args.canvas,
        "transition_check": ("enabled" if transitions and transitions_ok
                             else "skipped(empty_list)" if transitions_ok
                             else "skipped(input_error)"),
    }
    report["input_errors"] = input_errors

    os.makedirs(args.out, exist_ok=True)
    report_path = os.path.join(args.out, "report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    s = report["summary"]
    print(f"报告已写入 {report_path}")
    print(f"字幕 {s['total_subtitles']} 条(有效 {s['valid_subtitles']}),"
          f"时间倒置 {s['time_inversions']},转场冲突 {s['transition_conflicts']},"
          f"互叠配对 {s['overlap_pairs']},安全区越界 {s['safe_area_violations']}")
    if input_errors:
        print(f"输入告警 {len(input_errors)} 条(详见报告 input_errors)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
