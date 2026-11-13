"""命令行入口：scan（单目录扫描）与 compare（修复前后对比）。"""
from __future__ import annotations

import argparse
import json
import sys

from .compare import compare_scans
from .scanner import build_report, scan_directory


def _write_report(report: dict[str, object], output: str | None) -> None:
    """输出 JSON：指定 -o 写 UTF-8 文件；未指定则写到标准输出。"""
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False)
    if output:
        with open(output, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
            fh.write("\n")
    else:
        print(payload)


def _print_scan_summary(root: str, output: str | None) -> None:
    """控制台摘要（与 JSON 文件中的计数保持一致）。"""
    findings, skipped, scanned = scan_directory(root)
    lines = [
        f"[pysentry] 扫描目录: {root}",
        f"[pysentry] 已扫描 .py 文件数: {len(scanned)}",
        f"[pysentry] 跳过文件数: {len(skipped)}",
        f"[pysentry] 问题总数: {len(findings)}",
    ]
    if not scanned and not skipped:
        lines.append("[pysentry] 未发现可扫描目标：目录不存在或目录内没有 .py 文件")
    for item in skipped:
        lines.append(f"[pysentry]   跳过 {item['file']}（{item['reason']}）")
    text = "\n".join(lines)
    if output:
        # JSON 已写入文件，摘要走控制台
        print(text)
    else:
        # JSON 在标准输出，摘要走标准错误，避免污染可解析输出
        print(text, file=sys.stderr)


def _print_compare_summary(result: dict[str, object], output: str | None) -> None:
    lines = [
        f"[pysentry] 修复前问题数: {result['before_issue_count']}",
        f"[pysentry] 修复后问题数: {result['after_issue_count']}",
        f"[pysentry] 已修复问题: {result['fixed_count']}",
        f"[pysentry] 仍存在问题: {result['remaining_count']}",
    ]
    if result["introduced_issues"]:
        lines.append(f"[pysentry] 修复后新引入问题: {len(result['introduced_issues'])}")
    text = "\n".join(lines)
    if output:
        print(text)
    else:
        print(text, file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pysentry",
        description="离线静态检测：越权读取 / 递归资源耗尽 / 敏感信息泄露（仅单文件分析）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="扫描一个目录内的全部 .py 文件")
    p_scan.add_argument("directory", help="待扫描目录")
    p_scan.add_argument("-o", "--output", help="JSON 报告输出路径（UTF-8）；不指定则输出到标准输出")

    p_cmp = sub.add_parser("compare", help="对比修复前后两个目录的扫描结果")
    p_cmp.add_argument("before", help="修复前目录")
    p_cmp.add_argument("after", help="修复后目录")
    p_cmp.add_argument("-o", "--output", help="JSON 对比报告输出路径（UTF-8）")
    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认编码可能不是 UTF-8，显式切换以保证中文与 JSON 输出稳定
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    args = build_parser().parse_args(argv)
    if args.command == "scan":
        report = build_report(args.directory)
        _write_report(report, args.output)
        _print_scan_summary(args.directory, args.output)
        return 0

    result = compare_scans(args.before, args.after)
    _write_report(result, args.output)
    _print_compare_summary(result, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
