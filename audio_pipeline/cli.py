"""命令行入口。

用法：
    python process_audio.py --input <输入目录> --output <输出目录> --config <配置JSON>
                            [--report <报告JSON路径>]

退出码：
    0  全部素材处理成功（允许存在被跳过的无效素材）
    2  配置错误（此时不会创建任何输出）
    3  存在素材处理失败
    4  参数或输入目录错误
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 同时支持“包内运行”和把 process_audio.py 复制到项目根直接运行
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from audio_pipeline.config import ConfigError, load_config
    from audio_pipeline.pipeline import process_directory, write_report_file
else:
    from .config import ConfigError, load_config
    from .pipeline import process_directory, write_report_file

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_HAS_FAILURE = 3
EXIT_USAGE_ERROR = 4


def _force_utf8_stdio() -> None:
    """Windows 重定向到文件/管道时默认 GBK，强制 UTF-8 保证中文报告不乱码。

    优先用 PEP 528 的 reconfigure；在 python -I 隔离模式等 reconfigure
    不生效的场景，再通过 Win32 API 直接切换控制台代码页。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleOutputCP(65001)  # CP_UTF8
            kernel32.SetConsoleCP(65001)
        except Exception:
            pass


def _print_report(report) -> None:
    summary = report.summary
    print("=" * 64)
    print("音效批处理结果报告")
    print("=" * 64)
    print(
        f"输入目录: {report.input_dir}\n输出目录: {report.output_dir}\n"
        f"合计 {summary['total']} 个文件：成功 {summary['success']}，"
        f"跳过 {summary['skipped']}，失败 {summary['failed']}"
    )

    if report.success:
        print("-" * 64)
        print(f"[成功] {len(report.success)} 个：")
        for entry in report.success:
            print(f"  ✓ {entry.rel_path}  ->  {Path(entry.output_wav).name} "
                  f"(+ 同名 JSON, 时长 {entry.duration_seconds:.3f}s)")
            for step in entry.steps:
                print(f"      · {step.step}: {step.detail}")

    if report.skipped:
        print("-" * 64)
        print(f"[跳过] {len(report.skipped)} 个（无效素材，不影响其他文件）：")
        for entry in report.skipped:
            print(f"  ⊘ {entry.rel_path}  原因: {entry.reason}")

    if report.failed:
        print("-" * 64)
        print(f"[失败] {len(report.failed)} 个：")
        for entry in report.failed:
            print(f"  ✗ {entry.rel_path}  原因: {entry.reason}")

    print("=" * 64)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="游戏音效批处理：静音裁剪 / 淡入淡出 / 增益，并生成 JSON 元数据",
    )
    parser.add_argument("--input", "-i", required=True, help="输入音效目录（递归处理子目录）")
    parser.add_argument("--output", "-o", required=True, help="输出目录（保持相对子目录结构）")
    parser.add_argument("--config", "-c", required=True, help="JSON 配置文件路径")
    parser.add_argument("--report", "-r", help="可选：把完整 JSON 报告写到该路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)

    # 1) 先校验配置：配置不合法时直接退出，不创建/修改任何输出
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"配置错误，已取消处理（未处理任何素材）：{exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    if not Path(args.input).is_dir():
        print(f"输入目录不存在或不是目录：{args.input}", file=sys.stderr)
        return EXIT_USAGE_ERROR

    # 2) 配置无误后才开始处理素材
    try:
        report = process_directory(args.input, args.output, config)
    except NotADirectoryError as exc:
        print(f"运行错误：{exc}", file=sys.stderr)
        return EXIT_USAGE_ERROR
    except OSError as exc:
        print(f"运行错误（文件系统）：{exc}", file=sys.stderr)
        return EXIT_USAGE_ERROR

    _print_report(report)

    if args.report:
        write_report_file(report, args.report)
        print(f"完整 JSON 报告已写入: {args.report}")

    if report.failed:
        return EXIT_HAS_FAILURE
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
