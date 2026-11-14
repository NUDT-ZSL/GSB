"""命令行入口。"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from .config import Config, load_config
from .errors import ConfigError, CoverSelectorError, SetupError
from .manifest import load_manifest
from .pipeline import SelectionRun
from .report import report_filename, write_report_files


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cover-selector",
        description="按候选清单从逐帧 PNG 序列中挑选封面候选，输出排序后的候选图与核对报告。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python -m cover_selector --frames samples/frames --manifest samples/manifest.json "
            "--config samples/config.json --output out\n"
        ),
    )
    parser.add_argument("--frames", required=True, type=Path, help="帧序列目录")
    parser.add_argument("--manifest", required=True, type=Path, help="候选时间点清单（JSON）")
    parser.add_argument("--config", required=True, type=Path, help="配置文件（JSON）")
    parser.add_argument("--output", required=True, type=Path, help="输出目录（不存在时自动创建）")
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="覆盖配置中的 dedupe_threshold，便于对比不同阈值的效果",
    )
    parser.add_argument(
        "--resize-mode",
        choices=("fit", "stretch"),
        default=None,
        help="覆盖配置中的 resize_mode",
    )
    parser.add_argument(
        "--iteration",
        default=None,
        help="本次运行的名称，会写入报告文件名与报告内容，默认为 default",
    )
    parser.add_argument("--version", action="version", version=f"cover-selector {__version__}")
    return parser


def _apply_overrides(config: Config, args: argparse.Namespace) -> Config:
    if args.threshold is None and args.resize_mode is None:
        return config
    threshold = config.dedupe_threshold if args.threshold is None else args.threshold
    if not 0 <= threshold <= 1:
        raise ConfigError(f"--threshold 必须在 0 到 1 之间，实际为 {threshold:g}")
    return Config(
        fps=config.fps,
        base_width=config.base_width,
        base_height=config.base_height,
        dedupe_threshold=threshold,
        tolerance_frames=config.tolerance_frames,
        frame_start=config.frame_start,
        resize_mode=args.resize_mode or config.resize_mode,
    )


def _validate_output_dir(args: argparse.Namespace) -> None:
    frames = args.frames.resolve()
    output = args.output.resolve()
    if output == frames or frames in output.parents:
        raise SetupError("输出目录不能位于帧序列目录内，以免污染源帧")
    if output == args.manifest.resolve():
        raise SetupError("输出目录不能是候选清单文件自身")
    if output.exists() and not output.is_dir():
        raise SetupError(f"输出路径已存在且不是目录：{args.output}")
    if args.manifest.resolve().parent == output:
        reserved = {
            report_filename(args.iteration, ext).lower()
            for ext in ("json", "md", "html")
        }
        if args.manifest.resolve().name.lower() in reserved:
            raise SetupError("输出目录会覆盖候选清单文件，请换一个输出目录或运行名称")


def run(args: argparse.Namespace) -> int:
    started_at = datetime.now()
    _validate_output_dir(args)

    config = _apply_overrides(load_config(args.config), args)
    entries = load_manifest(args.manifest)
    run_task = SelectionRun(
        frame_dir=args.frames,
        manifest_path=args.manifest,
        entries=entries,
        config=config,
        output_dir=args.output,
        iteration=args.iteration,
    )
    run_task.prepare()
    report = run_task.execute(started_at=started_at)
    report_paths = write_report_files(report, args.output, report.iteration)

    counts = report.status_counts
    print(
        f"完成：清单 {len(entries)} 条，输出候选 {counts['selected']} 张，"
        f"合并 {counts['merged']} 条，未产出 {counts['skipped'] + counts['error']} 条"
    )
    print(f"输出目录：{args.output.resolve()}")
    for label in ("md", "html", "json"):
        print(f"  {label:4s} {report_paths[label].name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except CoverSelectorError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已取消", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
