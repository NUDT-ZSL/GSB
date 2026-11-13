"""命令行接口。

示例::

    python -m vuln_scanner --input samples/raw --output out/report.json
    python -m vuln_scanner -i samples/fixed --baseline out/report_raw.json -o out/report_fixed.json
    python -m vuln_scanner -i samples/raw --config config/rules.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .report import build_report, compare_with_baseline, write_report
from .scanner import Scanner
from .security import PathSecurityError, SecurityLogger, check_external_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vuln_scanner",
        description="本地文件导入组件漏洞检测：扫描目录内 JSON/CSV，输出漏洞清单 JSON 报告。",
    )
    parser.add_argument("-i", "--input", required=True,
                        help="待扫描的样本目录（只解析目录内 .json/.csv）")
    parser.add_argument("-o", "--output", default="report.json",
                        help="报告输出路径，缺省为当前目录 report.json")
    parser.add_argument("-c", "--config", default=None,
                        help="自定义危险规则配置（JSON），与内置规则合并使用")
    parser.add_argument("-b", "--baseline", default=None,
                        help="基线报告路径；提供后对当前结果做修复状态对比")
    parser.add_argument("-l", "--security-log", default=None,
                        help="安全日志路径，缺省为 <输出目录>/security.log.jsonl")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    input_dir = Path(args.input)
    output_path = Path(args.output)
    log_path = Path(args.security_log) if args.security_log \
        else output_path.parent / "security.log.jsonl"

    if not input_dir.exists() or not input_dir.is_dir():
        print(f"[错误] 输入目录不存在或不是目录: {input_dir}", file=sys.stderr)
        return 2

    security_logger = SecurityLogger(log_path)

    # 验收点 7：传入越界路径（绝对路径/父目录引用）时拒绝并写安全日志。
    # 这里对输入目录本身做一次边界自检演示；扫描中的每次文件读取也都经过安全解析。
    try:
        check_external_path(input_dir, "../escape_probe.txt", security_logger)
    except PathSecurityError as exc:
        print(f"[安全] 越界访问已拒绝并记录: {exc.reason}", file=sys.stderr)

    scanner = Scanner(input_dir, security_logger, config_path=args.config)
    result = scanner.scan()
    report = build_report(result)

    if args.baseline:
        baseline_path = Path(args.baseline)
        if not baseline_path.exists():
            print(f"[错误] 基线报告不存在: {baseline_path}", file=sys.stderr)
            return 2
        baseline_report = json.loads(baseline_path.read_text(encoding="utf-8"))
        report["baseline_comparison"] = compare_with_baseline(result, baseline_report)

    written = write_report(report, output_path)
    summary = report["summary"]
    print(
        f"[完成] 报告已写入: {written}\n"
        f"       命中 {summary['total_findings']} 项 "
        f"(高 {summary['high']}/中 {summary['medium']}/低 {summary['low']})，"
        f"解析失败 {summary['parse_failed']} 个，"
        f"拒绝/跳过 {summary['rejected_or_skipped']} 个，"
        f"安全日志: {log_path}"
    )
    if report["config_issues"]:
        for issue in report["config_issues"]:
            print(f"[配置告警] {issue}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
