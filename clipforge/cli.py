"""命令行入口：python -m clipforge --source SRC --manifest JSON --output OUT [--report R]"""

from __future__ import annotations

import argparse
import sys

from .processor import EXIT_INTERNAL, run
from .wavio import AudioError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clipforge",
        description="按清单裁剪 PCM WAV 并顺序拼接为成片。",
    )
    parser.add_argument("--source", required=True, help="源 PCM WAV 路径")
    parser.add_argument("--manifest", required=True, help="片段清单 JSON 路径")
    parser.add_argument("--output", required=True, help="成片输出 WAV 路径")
    parser.add_argument(
        "--report",
        default=None,
        help="report.json 路径（默认：输出目录下的 report.json）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run(args.source, args.manifest, args.output, args.report)
    except AudioError as exc:
        print(f"clipforge: {exc.code}: {exc.message}", file=sys.stderr)
        return EXIT_INTERNAL
    except Exception as exc:  # 未预期异常也不静默
        print(f"clipforge: internal_error: {exc}", file=sys.stderr)
        return EXIT_INTERNAL

    report = result.report
    summary = report.get("summary", {})
    if result.exit_code == 0:
        print(
            f"成片已生成：{report['output']['path']} "
            f"({report['output']['frames']} 帧 / "
            f"{report['output']['duration_seconds']:.3f} 秒)"
        )
        print(
            f"片段：共 {summary.get('total', 0)} 项，"
            f"已处理 {summary.get('processed', 0)} 项，"
            f"未处理 {summary.get('unprocessed', 0)} 项"
        )
        print(f"报告：{result.report_path}")
    else:
        errors = report.get("errors") or [{"code": "unknown", "message": "未知错误"}]
        print(f"任务失败（退出码 {result.exit_code}）", file=sys.stderr)
        for err in errors:
            print(f"  {err['code']}: {err['message']}", file=sys.stderr)
        if summary.get("unprocessed"):
            print(
                f"  未处理片段：{summary.get('unprocessed')} 项 "
                f"(索引 {summary.get('unprocessed_indices')})",
                file=sys.stderr,
            )
        print(f"报告：{result.report_path}", file=sys.stderr)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
