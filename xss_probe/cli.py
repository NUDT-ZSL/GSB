"""命令行入口。

退出码约定：
  0  扫描完成且不存在风险（或仅生成样本文件）
  1  扫描完成但存在风险
  2  参数错误
  3  目标不是本机回环地址或其他运行前校验失败
"""

from __future__ import annotations

import argparse
import json
import sys

from .models import RESULT_NO_RESPONSE, RESULT_RISK
from .netutil import NotLoopbackError, assert_loopback_url
from .report import (
    build_report,
    compare_reports,
    load_cases,
    load_report,
    save_report,
)
from .samples import write_history_sample, write_sample_cases
from .scanner import run_scan

EXIT_SAFE = 0
EXIT_RISK = 1
EXIT_USAGE = 2
EXIT_REJECTED = 3


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m xss_probe",
        description="本机回环反射型脚本注入探测工具（仅限 127.0.0.0/8、::1）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_cases = sub.add_parser("gen-cases", help="生成确定性探测用例清单 JSON")
    p_cases.add_argument("--output", "-o", required=True, help="输出文件路径")

    p_hist = sub.add_parser("gen-history", help="生成确定性历史报告样本 JSON")
    p_hist.add_argument("--output", "-o", required=True, help="输出文件路径")

    p_scan = sub.add_parser("scan", help="对本机站点执行探测并输出 JSON 报告")
    p_scan.add_argument("--base-url", required=True, help="本地站点基址，必须是回环地址")
    p_scan.add_argument("--cases", required=True, help="探测用例清单 JSON 路径")
    p_scan.add_argument("--report", required=True, help="本次报告输出路径")
    p_scan.add_argument(
        "--fix-status", required=True,
        help="本次运行的修复状态标识，如 vulnerable / fixed-2026-11-14",
    )
    p_scan.add_argument("--history", help="可选：历史报告路径，用于新增/消除风险比对")
    p_scan.add_argument("--timeout", type=float, default=5.0, help="单请求超时秒数")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "gen-cases":
        path = write_sample_cases(args.output)
        print(f"已生成探测用例清单: {path}")
        return EXIT_SAFE

    if args.command == "gen-history":
        path = write_history_sample(args.output)
        print(f"已生成历史报告样本: {path}")
        return EXIT_SAFE

    # ---- scan ----
    try:
        assert_loopback_url(args.base_url)
    except NotLoopbackError as exc:
        print(f"拒绝执行：{exc}", file=sys.stderr)
        return EXIT_REJECTED

    try:
        cases = load_cases(args.cases)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"用例清单加载失败: {exc}", file=sys.stderr)
        return EXIT_USAGE

    records = run_scan(args.base_url, cases, timeout=args.timeout)
    report = build_report(args.base_url, args.fix_status, records)

    comparison = None
    if args.history:
        try:
            history = load_report(args.history)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"历史报告加载失败: {exc}", file=sys.stderr)
            return EXIT_USAGE
        comparison = compare_reports(history, report)
        report["历史比对"] = comparison

    out_path = save_report(report, args.report)

    summary = report["汇总"]
    print("=" * 60)
    print(f"目标基址    : {report['目标基址']}")
    print(f"修复状态标识: {report['修复状态标识']}")
    print(f"运行标识    : {report['运行标识']}")
    print(f"报告路径    : {out_path}")
    print("-" * 60)
    print(f"总用例数            : {summary['总用例数']}")
    print(f"存在风险数          : {summary['存在风险数']}")
    print(f"未获得有效响应数    : {summary['未获得有效响应数']}")
    print(f"无风险数            : {summary['无风险数']}")
    if comparison is not None:
        print("-" * 60)
        print(f"新增风险数: {comparison['新增风险数']}")
        for item in comparison["新增风险"]:
            print(f"  [新增] {item['用例标识']} {item['入口类型']} "
                  f"上下文={item['上下文类型']} 标记={item['标记']}")
        print(f"已消除风险数: {comparison['已消除风险数']}")
        for item in comparison["已消除风险"]:
            print(f"  [消除] {item['用例标识']} {item['入口类型']} "
                  f"上下文={item['上下文类型']} 标记={item['标记']}")
    print("=" * 60)

    if summary["存在风险数"] > 0:
        print("结论: 存在风险，退出码 1")
        return EXIT_RISK
    print("结论: 未发现风险，退出码 0")
    return EXIT_SAFE


if __name__ == "__main__":
    sys.exit(main())
