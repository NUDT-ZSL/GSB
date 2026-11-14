"""命令行入口。

用法::

    python -m beat_align.cli --wav music.wav --shots shots.json --output-dir previews

退出码：

* 0  正常完成（含"预览目录不可写但 JSON 已产出"这类已上报的部分失败）；
* 2  业务错误（WAV 不支持 / 读取失败、清单非法等），错误信息打印到 stderr；
* 1  未预期的程序错误，附堆栈。
"""

from __future__ import annotations

import argparse
import sys

from .detect import DEFAULT_FRAME_MS, DEFAULT_HOP_MS, DEFAULT_MIN_GAP_MS
from .errors import BeatAlignError
from .pipeline import run_pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="beat-align",
        description="离线鼓点检出与镜头切点对齐工具：读 WAV + 镜头清单，"
        "输出对齐预览图与节奏核查 JSON。",
    )
    parser.add_argument("--wav", required=True, help="本地单声道 PCM WAV 配乐路径")
    parser.add_argument("--shots", required=True, help="镜头清单 JSON 路径")
    parser.add_argument("--output-dir", required=True, help="切换预览图输出目录")
    parser.add_argument(
        "--report",
        default=None,
        help="核查结果 JSON 路径（默认写到预览目录同级的 <目录名>_rhythm_report.json）",
    )
    parser.add_argument("--frame-ms", type=int, default=DEFAULT_FRAME_MS, help="分析窗长毫秒，默认 20")
    parser.add_argument("--hop-ms", type=int, default=DEFAULT_HOP_MS, help="帧移毫秒，默认 10")
    parser.add_argument(
        "--min-gap-ms",
        type=int,
        default=DEFAULT_MIN_GAP_MS,
        help="相邻节奏点最小间隔毫秒，小于该值的重复检出会被合并，默认 120",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_pipeline(
            wav_path=args.wav,
            shots_path=args.shots,
            output_dir=args.output_dir,
            report_path=args.report,
            frame_ms=args.frame_ms,
            hop_ms=args.hop_ms,
            min_gap_ms=args.min_gap_ms,
        )
    except BeatAlignError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2

    report = result.report
    print("=" * 68)
    print("鼓点对齐流水线执行完成")
    print("=" * 68)
    print(f"配乐信息   : {result.audio.describe()}")
    print(f"鼓点数量   : {len(result.beats)}")
    print(f"鼓点时间ms : {[b.time_ms for b in result.beats]}")
    print(f"总长核查   : {report['overhang']['message']}")
    print(
        f"镜头统计   : 共 {report['summary']['total_shots']} 条，"
        f"已对齐 {report['summary']['completed_count']} 条，"
        f"未完成 {report['summary']['incomplete_count']} 条"
    )
    for item in report["completed"]:
        print(
            f"  #{item['index']:02d} {item['title']}: "
            f"{item['original_start_ms']} -> {item['aligned_start_ms']} ms "
            f"(偏移 {item['offset_ms']} ms, {item['direction_label']}, "
            f"鼓点 {item['beat_time_ms']} ms)"
        )
    for item in report["incomplete"]:
        print(f"  #{item['index']:02d} {item['title']}: 未完成（{item['reason']}）")
    print(f"核查 JSON  : {result.report_path}")
    if result.preview_error:
        print(f"[警告] 预览图未生成：{result.preview_error}", file=sys.stderr)
        print("        核查结果 JSON 已正常写出并包含全部条目，可直接打开核对。")
    else:
        print(f"预览图     : {len(result.preview_paths)} 张，目录 {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
