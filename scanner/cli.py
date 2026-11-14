"""命令行入口：generate / scan / compare 三个子命令。

全程仅读写本地文件，不发起任何网络请求。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .compare import compare_reports
from .engine import ManifestError, scan
from .report_io import write_report
from .sample_generator import generate


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="secretscan",
        description="本地 HTTP 服务配置与响应样本敏感信息暴露自查工具（离线、确定性输出）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="确定性生成样本、损坏样本与清单")
    gen.add_argument("target", type=Path, help="样本工作目录（将在其中创建 samples/ 与清单）")

    scn = sub.add_parser("scan", help="按清单扫描样本目录并输出 JSON 报告")
    scn.add_argument("scan_root", type=Path, help="包含 manifest.json 的样本目录")
    scn.add_argument("--output", "-o", type=Path, required=True, help="报告输出路径（JSON）")

    cmp_cmd = sub.add_parser("compare", help="比对修复前后两份报告，确认暴露是否已消除")
    cmp_cmd.add_argument("baseline", type=Path, help="修复前报告（JSON）")
    cmp_cmd.add_argument("current", type=Path, help="修复后报告（JSON）")
    cmp_cmd.add_argument("--output", "-o", type=Path, required=True, help="比对结果输出路径（JSON）")
    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认编码可能是 GBK，强制 UTF-8 输出避免中文报错
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    args = _build_parser().parse_args(argv)

    if args.command == "generate":
        written = generate(args.target)
        print(f"已生成 {len(written)} 个文件到 {args.target}")
        for path in written:
            print(f"  - {path}")
        return 0

    if args.command == "scan":
        try:
            report = scan(args.scan_root)
        except ManifestError as exc:
            print(f"扫描中止：{exc}", file=sys.stderr)
            return 2
        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_report(report, args.output)
        summary = report["summary"]
        print(
            "扫描完成：声明 {declared_samples} 个，完成 {completed_samples} 个，"
            "未完成 {incomplete_samples} 个；已确认 {confirmed_samples} 个，"
            "可疑 {suspected_samples} 个，合规 {clean_samples} 个；暴露条目共 {total_findings} 条。".format(
                **summary
            )
        )
        print(f"报告已写入：{args.output}")
        return 0

    if args.command == "compare":
        result = compare_reports(args.baseline, args.current)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_report(result, args.output)
        summary = result["summary"]
        print(
            "比对完成：已消除 {removed} 条，未消除 {present} 条，新增 {new} 条；"
            "未完成样本 {incomplete_samples} 个。".format(**summary)
        )
        print(f"比对结果已写入：{args.output}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
