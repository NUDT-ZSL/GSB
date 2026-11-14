"""命令行入口。

用法示例::

    python -m sanitizer_audit.cli --log-dir samples/logs --report out/report.json
    python -m sanitizer_audit.cli --log-dir samples/logs --report out/report.json \\
        --fail-fixed-call 1 --max-attempts 2
    python -m sanitizer_audit.cli --log-dir samples/logs --report out/report.json \\
        --disable-fixed

退出码：
  0  正常完成（所有文件均已核查）
  1  报告已写出，但存在未能核查的文件（实现不可用 / 编码异常 / 解析异常）
  2  参数或目录问题（目录不存在、无 .log 文件），未写报告
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sanitizer_audit.core import (
    AuditError,
    FaultPlan,
    LogDirectoryMissing,
    NoLogFiles,
    build_report,
    write_report,
)

EXIT_OK = 0
EXIT_INCOMPLETE = 1
EXIT_FATAL = 2


def _print_summary(report: dict) -> None:
    summary = report["summary"]
    print(f"日志目录: {report['log_dir']}")
    print(
        "文件总数: {total_files}  已确认漏洞: {confirmed_vulnerabilities}  "
        "未验证: {unverified}  修复后未验证: {fix_unverified}  "
        "编码异常: {encoding_errors}  解析异常: {parse_errors}".format(**summary)
    )
    print(
        "残余总数: 修复前 {legacy_residual_total} / 修复后 {fixed_residual_total}；"
        "修复后未验证: {fix_unverified}".format(**summary)
    )

    for record in report["records"]:
        status = record["status"]
        line_count = record["line_count"]
        print(
            f"  [{status}] {record['path']}"
            f"  行数={line_count if line_count is not None else '-'}"
            f"  残留(修复前)={record['legacy_residual_count']}"
            f"  残留(修复后)={record['fixed_residual_count']}"
        )
        for item in record["residuals"]:
            print(f"      残留 {item['type']} @ 行 {item['line']}")
        if record["error"]:
            print(f"      说明: {record['error']}")

    confirmed = report["confirmed_vulnerabilities"]
    if confirmed:
        print("已确认漏洞文件: " + ", ".join(confirmed))
    affected = report["incomplete"]["affected_files"]
    if affected:
        print("未能完整核查的文件: " + ", ".join(affected))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="扫描日志目录，核查脱敏后残留的手机号与证件号。"
    )
    parser.add_argument("--log-dir", required=True, help="待扫描的日志目录")
    parser.add_argument("--report", required=True, help="JSON 报告输出路径")
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=2,
        help="单套实现处理单个文件的最大尝试次数（默认 2）",
    )
    parser.add_argument(
        "--fail-fixed-call",
        type=int,
        default=None,
        metavar="N",
        help="模拟修复后实现第 N 次调用失败，用于演示重试",
    )
    parser.add_argument(
        "--disable-fixed",
        action="store_true",
        help="模拟修复后实现持续不可用",
    )
    parser.add_argument(
        "--disable-legacy",
        action="store_true",
        help="模拟修复前实现持续不可用",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.max_attempts < 1:
        print("错误: --max-attempts 必须 >= 1", file=sys.stderr)
        return EXIT_FATAL

    fault_plan = FaultPlan(
        legacy_available=not args.disable_legacy,
        fixed_available=not args.disable_fixed,
        fail_fixed_call=args.fail_fixed_call,
    )

    try:
        report = build_report(
            Path(args.log_dir),
            fault_plan=fault_plan,
            max_attempts=args.max_attempts,
        )
    except (LogDirectoryMissing, NoLogFiles) as exc:
        print(f"提示: {exc}", file=sys.stderr)
        return EXIT_FATAL
    except AuditError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return EXIT_FATAL
    except OSError as exc:
        print(f"错误: 无法读取日志目录: {exc}", file=sys.stderr)
        return EXIT_FATAL

    report_path = write_report(report, Path(args.report))
    _print_summary(report)
    print(f"报告已写入: {report_path}")

    summary = report["summary"]
    incomplete = (
        summary["unverified"]
        + summary["fix_unverified"]
        + summary["encoding_errors"]
        + summary["parse_errors"]
    )
    return EXIT_INCOMPLETE if incomplete else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
