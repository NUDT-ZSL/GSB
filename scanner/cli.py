# -*- coding: utf-8 -*-
"""命令行入口（任务9：无参使用当前目录与默认配置，带参按指定路径运行）。

用法示例：
  python -m scanner                                  # 扫描当前目录，默认配置
  python -m scanner --dir samples/vulnerable --report out/report.json
  python -m scanner --sim-config sim.json --no-sim
  python -m scanner --baseline out/baseline.json --dir samples/fixed
  python -m scanner --read-only                      # 只读目录模拟（任务10）
  python -m scanner --conflict                       # 预置同名文件（任务4）
  python -m scanner --content "hello" --payload "../x.txt"
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# 支持 `python scanner/cli.py` 与 `python -m scanner` 两种启动方式
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from scanner.detector import scan_directory
    from scanner.reporter import (build_report, diff_reports, render_text_summary,
                                   write_report)
    from scanner.simulator import (DEFAULT_CONTENT, DEFAULT_MARKER_NAME,
                                   DEFAULT_PAYLOAD, SimConfig, simulate_all)
else:
    from .detector import scan_directory
    from .reporter import (build_report, diff_reports, render_text_summary,
                           write_report)
    from .simulator import (DEFAULT_CONTENT, DEFAULT_MARKER_NAME,
                            DEFAULT_PAYLOAD, SimConfig, simulate_all)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scanner",
        description="Python 文件上传服务路径穿越静态检测与模拟利用工具",
    )
    p.add_argument("--dir", "-d", default=".", help="待扫描源码目录（默认当前目录）")
    p.add_argument("--report", "-o", default=None,
                   help="JSON 报告输出路径（默认 out/report.json）")
    p.add_argument("--text", default=None,
                   help="文本摘要输出路径（默认与报告同目录同名 .txt）")
    p.add_argument("--sim-config", default=None,
                   help="模拟配置 JSON 文件路径（缺省使用内置默认配置）")
    p.add_argument("--no-sim", action="store_true", help="只做静态扫描，跳过模拟利用")
    p.add_argument("--baseline", default=None,
                   help="修复前基线报告（JSON），用于 fixed_count/remaining_count 对比")
    p.add_argument("--payload", default=None, help="覆盖配置：恶意文件名（含 ../）")
    p.add_argument("--marker-name", default=None, help="覆盖配置：逃逸标记文件名")
    p.add_argument("--content", default=None, help="覆盖配置：上传内容（字符串，UTF-8）")
    p.add_argument("--content-file", default=None,
                   help="覆盖配置：从文件读取上传内容（逐字节）")
    p.add_argument("--conflict", action="store_true",
                   help="模拟前在目标路径预置同名文件，验证保留原文件（任务4）")
    p.add_argument("--read-only", action="store_true",
                   help="把沙箱上传目录设为只读后模拟（任务10），失败时退出码=2")
    p.add_argument("--sandbox-base", default=None,
                   help="沙箱根目录（默认系统临时目录）")
    return p


def resolve_config(args: argparse.Namespace) -> SimConfig:
    cfg = SimConfig()
    if args.sim_config:
        with open(args.sim_config, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if "marker_name" in data:
            cfg.marker_name = data["marker_name"]
        if "payload" in data:
            cfg.payload = data["payload"]
        if "content" in data:
            cfg.content = data["content"] if isinstance(data["content"], bytes) \
                else str(data["content"]).encode("utf-8")
        if "entry_candidates" in data:
            cfg.entry_candidates = tuple(data["entry_candidates"])
        cfg.precreate_conflict = bool(data.get("precreate_conflict", False))
        cfg.read_only = bool(data.get("read_only", False))
    # 命令行覆盖优先级最高
    if args.marker_name:
        cfg.marker_name = args.marker_name
        if not args.payload:
            cfg.payload = "../" + cfg.marker_name
    if args.payload:
        cfg.payload = args.payload
    if args.content is not None:
        cfg.content = args.content.encode("utf-8")
    if args.content_file:
        with open(args.content_file, "rb") as fh:
            cfg.content = fh.read()
    if args.conflict:
        cfg.precreate_conflict = True
    if args.read_only:
        cfg.read_only = True
    return cfg


def config_to_dict(cfg: SimConfig) -> dict:
    return {
        "marker_name": cfg.marker_name,
        "payload": cfg.payload,
        "content_utf8": cfg.content.decode("utf-8", errors="replace"),
        "content_length": len(cfg.content),
        "entry_candidates": list(cfg.entry_candidates),
        "precreate_conflict": cfg.precreate_conflict,
        "read_only": cfg.read_only,
    }


def load_baseline_findings(path: str):
    """从基线 JSON 重建用于对比的轻量 Finding 列表。"""
    from scanner.detector import Finding
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    out = []
    for v in data.get("vulnerabilities", []):
        out.append(Finding(
            file=v["file"], line=v["line"], severity=v["severity"],
            function=v.get("function", ""), sink=v.get("sink", ""),
            taint_level=2, taint_source=v.get("taint_source", ""),
            code=v.get("code", ""), suggestion=v.get("suggestion", ""),
            fingerprint=v["fingerprint"], id=v["id"],
        ))
    return out


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report_path = args.report or os.path.join("out", "report.json")
    text_path = args.text or os.path.splitext(report_path)[0] + ".txt"

    if not os.path.isdir(args.dir):
        print(f"[错误] 扫描目录不存在或不是目录: {args.dir}", file=sys.stderr)
        return 2

    scan = scan_directory(args.dir)
    cfg = resolve_config(args)

    sim_results = None
    if not args.no_sim and scan.findings:
        sim_results = simulate_all(os.path.abspath(args.dir), scan.findings,
                                   cfg, args.sandbox_base)
        # 只读模式（任务10）：存在因权限失败的写入模拟 -> 明确提示 + 非零退出
        perm_failures = [r for r in sim_results
                         if cfg.read_only and r.error and "权限不足" in r.error]
        if perm_failures:
            comparison = _maybe_compare(args, scan)
            report = build_report(scan, sim_results, config_to_dict(cfg), comparison)
            write_report(report, report_path)
            with open(text_path, "w", encoding="utf-8") as fh:
                fh.write(render_text_summary(report))
            print("[权限错误] 上传目录为只读，模拟上传无法写入：", file=sys.stderr)
            for r in perm_failures:
                print(f"  - {r.finding_id} {r.file}: {r.error}", file=sys.stderr)
            print(f"报告已写入: {report_path}", file=sys.stderr)
            return 2

    comparison = _maybe_compare(args, scan)
    report = build_report(scan, sim_results, config_to_dict(cfg), comparison)
    write_report(report, report_path)
    text = render_text_summary(report)
    with open(text_path, "w", encoding="utf-8") as fh:
        fh.write(text)

    print(text)
    print(f"\n[完成] JSON 报告: {os.path.abspath(report_path)}")
    print(f"[完成] 文本摘要: {os.path.abspath(text_path)}")
    if scan.errors:
        print(f"[提示] {len(scan.errors)} 个文件因错误被跳过，详见报告 errors 字段。")
    return 0


def _maybe_compare(args, scan):
    if not args.baseline:
        return None
    baseline_findings = load_baseline_findings(args.baseline)
    return diff_reports(
        _ScanLike(baseline_findings), scan
    )


class _ScanLike:
    def __init__(self, findings):
        self.findings = findings


if __name__ == "__main__":
    raise SystemExit(main())
