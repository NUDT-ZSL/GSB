"""命令行入口：setup / scan / compare / selfcheck。

退出码约定：
- 0  正常完成（含“部分覆盖”，未完成项在报告与摘要中显式标注）；
- 1  配置/用法错误或自检发现失败；
- 2  基线不可达，扫描未开始；
- 3  其他未预期错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .compare import build_comparison, load_report, write_comparison
from .config import ConfigError, load_config
from .report import build_report, canonical_json, write_report, write_text_summary
from .scanner import BaselineUnreachable, run_scan
from .selfcheck import main as selfcheck_main
from .setup import setup_workspace
from .state import ScanState, target_fingerprint

_EXIT_OK = 0
_EXIT_USAGE = 1
_EXIT_UNREACHABLE = 2
_EXIT_UNEXPECTED = 3


def _default_paths(workspace: Path, target_path: Path) -> tuple[Path, Path, Path]:
    stem = target_path.stem
    report_dir = workspace / "reports"
    state_dir = workspace / "state"
    return (
        report_dir / f"{stem}_report.json",
        report_dir / f"{stem}_summary.txt",
        state_dir / f"{stem}_state.json",
    )


def cmd_setup(args: argparse.Namespace) -> int:
    created = setup_workspace(args.workspace)
    print(f"实验环境已生成于 {Path(args.workspace).resolve()}，共 {len(created)} 个文件：")
    for p in sorted(created, key=lambda x: x.name):
        print(f"  - {p.name}")
    return _EXIT_OK


def cmd_scan(args: argparse.Namespace) -> int:
    target_path = Path(args.config)
    try:
        cfg = load_config(target_path)
    except ConfigError as exc:
        print("目标描述文件校验失败，未发起任何探测：", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return _EXIT_USAGE

    workspace = Path(args.workspace) if args.workspace else target_path.parent
    report_path = Path(args.report) if args.report else None
    summary_path = Path(args.summary) if args.summary else None
    state_path = Path(args.state) if args.state else None
    if report_path is None or summary_path is None or state_path is None:
        d_report, d_summary, d_state = _default_paths(workspace, target_path)
        report_path = report_path or d_report
        summary_path = summary_path or d_summary
        state_path = state_path or d_state

    fp = target_fingerprint(cfg)
    state = ScanState.load(state_path, fp)
    try:
        outcome = run_scan(cfg, state)
    except BaselineUnreachable as exc:
        print(f"可达性检查失败，扫描结束：{exc}", file=sys.stderr)
        return _EXIT_UNREACHABLE

    state.save(state_path)
    rep = build_report(cfg, outcome)
    write_report(rep, report_path)
    write_text_summary(rep, summary_path)

    print(rep["business_summary"])
    print()
    print(f"JSON 报告: {report_path.resolve()}")
    print(f"文本摘要: {summary_path.resolve()}")
    print(f"续扫状态: {state_path.resolve()}")
    if not outcome.summary["coverage_complete"]:
        print("提示：存在未完成探测，恢复服务后执行同一命令即可续扫补齐。")
    return _EXIT_OK


def cmd_compare(args: argparse.Namespace) -> int:
    target_path = Path(args.config)
    try:
        cfg = load_config(target_path)
    except ConfigError as exc:
        print("目标描述文件校验失败，未发起任何探测：", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return _EXIT_USAGE

    before = load_report(args.before)
    # 对照复测使用独立的内存状态（对照报告本身只承载本次复测结果）
    state = ScanState(target_fingerprint(cfg))
    try:
        outcome = run_scan(cfg, state)
    except BaselineUnreachable as exc:
        print(f"复测目标不可达，对照失败：{exc}", file=sys.stderr)
        return _EXIT_UNREACHABLE

    cmp_report = build_comparison(cfg, before, outcome)
    out_path = Path(args.output)
    write_comparison(cmp_report, out_path)
    print(cmp_report["business_summary"])
    print()
    for c in cmp_report["comparisons"]:
        print(f"  - {c['name']}: {c['conclusion']}（{c['conclusion_text']}）")
    print(f"\n对照报告: {out_path.resolve()}")
    return _EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m sqli_audit",
        description="本地订单查询接口 SQL 注入自查工具（仅回环目标 / 确定性样本 / 标准库实现）",
    )
    parser.add_argument("--version", action="version", version=f"sqli-audit {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_setup = sub.add_parser("setup", help="确定性生成实验环境（样本库/接口脚本/目标配置）")
    p_setup.add_argument("workspace", help="实验环境目录")
    p_setup.set_defaults(func=cmd_setup)

    p_scan = sub.add_parser("scan", help="对目标执行注入自查（支持断点续扫）")
    p_scan.add_argument("config", help="目标描述文件 target_*.json")
    p_scan.add_argument("--workspace", help="工作区目录（默认取配置文件所在目录）")
    p_scan.add_argument("--report", help="JSON 报告输出路径")
    p_scan.add_argument("--summary", help="业务文本摘要输出路径")
    p_scan.add_argument("--state", help="续扫状态文件路径")
    p_scan.set_defaults(func=cmd_scan)

    p_cmp = sub.add_parser("compare", help="修复对比：对修复后接口复测并与修复前报告对照")
    p_cmp.add_argument("config", help="修复后接口的目标描述文件")
    p_cmp.add_argument("--before", required=True, help="修复前扫描报告 JSON")
    p_cmp.add_argument("--output", required=True, help="对照报告输出路径")
    p_cmp.set_defaults(func=cmd_compare)

    p_self = sub.add_parser("selfcheck", help="内置确定性样本自检")
    p_self.set_defaults(func=lambda a: selfcheck_main())
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("用户中断", file=sys.stderr)
        return _EXIT_UNEXPECTED
    except Exception as exc:  # 防御性兜底：单进程异常不应吞掉，明确报错并返回非零
        print(f"未预期错误: {type(exc).__name__}: {exc}", file=sys.stderr)
        return _EXIT_UNEXPECTED


if __name__ == "__main__":
    raise SystemExit(main())
