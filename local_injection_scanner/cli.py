"""命令行入口。

用法示例：
  python -m local_injection_scanner --target http://127.0.0.1:8000 \
      --report report.json --params username,item --delay 10 --log scan.log

退出码：
  0  目标可达且未发现缺陷
  1  发现注入缺陷
  2  参数或目标地址错误（含非本机目标）
  3  目标服务持续不可达
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# 同时支持 `python -m local_injection_scanner.cli` 与直接以脚本路径运行
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from local_injection_scanner.http_client import NonLocalTargetError, assert_local_target
    from local_injection_scanner.logger import TimestampLogger
    from local_injection_scanner.scanner import scan_target
else:
    from .http_client import NonLocalTargetError, assert_local_target
    from .logger import TimestampLogger
    from .scanner import scan_target

DEFAULT_PARAMS = ["username", "item"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="local_injection_scanner",
        description="本机注入缺陷验证工具（仅允许访问 127.0.0.1 / localhost 目标）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--target",
        help="目标地址（必填），仅限本机，例如 http://127.0.0.1:8000 或 http://localhost:8000",
    )
    parser.add_argument(
        "--report",
        help="JSON 报告输出路径（必填），例如 report.json",
    )
    parser.add_argument(
        "--params",
        default=",".join(DEFAULT_PARAMS),
        help="待检测查询参数，逗号分隔（默认：username,item）",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="可选延迟探测：服务暂不可用时最长等待秒数，等待期内轮询，服务恢复即开始检测",
    )
    parser.add_argument(
        "--log",
        default=None,
        help="可选日志文件路径，记录带时间戳的扫描过程关键事件",
    )
    return parser


def ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)


def print_console_summary(report: dict) -> None:
    """控制台扫描摘要（验收第 12 条）。"""
    summary = report["summary"]
    print("=" * 60)
    print("扫描摘要")
    print("=" * 60)
    print(f"目标地址      : {report['target']} ({report['target_host']})")
    print(f"服务可达      : {'是' if report['service_reachable'] else '否'}")
    print(f"已检测参数数  : {summary['parameters_tested_count']}")
    print(f"发现缺陷数    : {summary['findings_count']}")
    if report["service_reachable"]:
        print(f"安全参数      : {summary['safe_parameters'] or '无'}")
        print(f"出错参数      : {summary['errored_parameters'] or '无'}")
    for finding in report["findings"]:
        print(
            f"  - 参数 {finding['parameter']!r} 存在 {finding['injection_type']}"
            f"（载荷: {finding['payload']!r}）"
        )
    print(f"结论          : {report['conclusion']}")
    print("=" * 60)


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 cp936，统一为 UTF-8 以正确显示中文报告与日志
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass

    parser = build_parser()
    # 无参数运行：argparse 默认打印 usage 并以退出码 2 结束
    args = parser.parse_args(argv)

    if not args.target or not args.report:
        parser.print_help()
        print("\n错误：--target 与 --report 为必填项。", file=sys.stderr)
        return 2

    # 1) 本机限制：非本机地址直接拒绝（验收第 11 条）
    try:
        assert_local_target(args.target)
    except NonLocalTargetError:
        print("仅支持本地目标", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"目标错误：{exc}", file=sys.stderr)
        return 2

    parameters = [p.strip() for p in args.params.split(",") if p.strip()]
    if not parameters:
        print("目标错误：--params 至少需要一个参数名", file=sys.stderr)
        return 2

    # 2) 输出路径与日志准备
    try:
        ensure_parent_dir(args.report)
        if args.log:
            ensure_parent_dir(args.log)
    except OSError as exc:
        print(f"输出路径错误：{exc}", file=sys.stderr)
        return 2

    logger = TimestampLogger(args.log)
    try:
        logger.info(
            f"开始扫描：target={args.target} params={parameters} "
            f"delay={args.delay}s report={args.report}"
        )
        report = scan_target(args.target, parameters, args.delay, logger)

        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        logger.info(f"JSON 报告已写入 {args.report}")

        print_console_summary(report)

        if not report["service_reachable"]:
            logger.error("目标服务持续不可达")
            return 3
        if report["findings"]:
            return 1
        return 0
    finally:
        logger.close()


if __name__ == "__main__":
    sys.exit(main())
