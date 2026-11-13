# -*- coding: utf-8 -*-
"""JSON 报告、文本摘要、修复前后对比。"""
from __future__ import annotations

import json
import os
from dataclasses import asdict

from . import rules as R
from .detector import Finding, ScanResult
from .simulator import SimResult, result_to_dict


def sort_findings(findings: list[Finding]) -> list[Finding]:
    """severity 降序（高危在前），同级按 file 字母序，再按行号，保证确定性。"""
    return sorted(findings,
                  key=lambda f: (R.SEVERITY_ORDER[f.severity], f.file, f.line,
                                 f.sink, f.fingerprint))


def build_report(scan: ScanResult, sim_results: list[SimResult] | None,
                 config: dict | None = None,
                 comparison: dict | None = None) -> dict:
    findings = sort_findings(scan.findings)
    sim_by_id = {r.finding_id: r for r in (sim_results or [])}
    items = []
    for f in findings:
        item = f.to_dict()
        if f.id in sim_by_id:
            item["simulation"] = result_to_dict(sim_by_id[f.id])
        items.append(item)
    report = {
        "schema_version": "1.0",
        "summary": {
            "total": len(findings),
            "high": sum(1 for f in findings if f.severity == R.SEVERITY_HIGH),
            "medium": sum(1 for f in findings if f.severity == R.SEVERITY_MEDIUM),
            "low": sum(1 for f in findings if f.severity == R.SEVERITY_LOW),
            "exploitable": sum(1 for r in (sim_results or []) if r.exploitable),
            "errors": len(scan.errors),
        },
        "config": config or {},
        "vulnerabilities": items,
        "errors": scan.errors,
        "scanned_files": sorted(scan.scanned_files),
    }
    if comparison is not None:
        report["comparison"] = comparison
    return report


def write_report(report: dict, path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)


def render_text_summary(report: dict) -> str:
    """文本摘要：逐条列出漏洞编号与状态。"""
    lines = []
    s = report["summary"]
    lines.append("=" * 72)
    lines.append("路径穿越扫描报告摘要")
    lines.append("=" * 72)
    lines.append(f"扫描文件数 : {len(report['scanned_files'])}")
    lines.append(f"漏洞总数   : {s['total']}（高危 {s['high']} / 中危 {s['medium']} / 低危 {s['low']}）")
    lines.append(f"模拟可利用 : {s['exploitable']}")
    lines.append(f"解析错误   : {s['errors']}")
    lines.append("-" * 72)
    if not report["vulnerabilities"]:
        lines.append("未检出路径穿越漏洞。")
    for v in report["vulnerabilities"]:
        lines.append(f"[{v['id']}] {v['severity']} {v['file']}:{v['line']} "
                     f"函数={v['function']} sink={v['sink']}")
        lines.append(f"      代码: {v['code']}")
        lines.append(f"      污点: {v['taint_source']}（{v['taint_level']}）")
        sim = v.get("simulation")
        if sim:
            flag = "可利用" if sim["exploitable"] else "未利用成功"
            lines.append(f"      模拟: payload={sim['payload']} -> {flag} "
                         f"(逃逸出上传根={sim['outside_upload_root']}, "
                         f"内容逐字节一致={sim['content_matches']})")
            if sim.get("conflict"):
                c = sim["conflict"]
                lines.append(f"      冲突: 目标已存在同名文件 {c['target']}，"
                             f"已跳过写入、原文件保留"
                             f"(overwritten={c['overwritten']}, "
                             f"content_preserved={c.get('content_preserved', True)})")
            if sim.get("error"):
                lines.append(f"      模拟错误: {sim['error']}")
        lines.append(f"      建议: {v['suggestion']}")
    if report["errors"]:
        lines.append("-" * 72)
        lines.append("已跳过的错误文件：")
        for e in report["errors"]:
            lines.append(f"  - {e['file']}: {e['error']} {e['message']}")

    comp = report.get("comparison")
    if comp is not None:
        lines.append("-" * 72)
        lines.append("修复前后对比：")
        lines.append(f"  fixed_count     = {comp['fixed_count']}")
        lines.append(f"  remaining_count = {comp['remaining_count']}")
        lines.append(f"  new_count       = {comp['new_count']}")
        for row in comp["per_vulnerability"]:
            lines.append(f"  - {row['id']} [{row['severity']}] {row['file']}:{row['line']} "
                         f"-> {row['status']}")
    lines.append("=" * 72)
    return "\n".join(lines)


def diff_reports(baseline: ScanResult, current: ScanResult) -> dict:
    """以稳定指纹对比修复前后两次扫描结果。"""
    base_map = {f.fingerprint: f for f in baseline.findings}
    cur_map = {f.fingerprint: f for f in current.findings}

    per_vuln = []
    fixed, remaining = 0, 0
    for f in sort_findings(baseline.findings):
        if f.fingerprint in cur_map:
            status = "未修复"
            remaining += 1
        else:
            status = "已修复"
            fixed += 1
        per_vuln.append({
            "id": f.id,
            "file": f.file,
            "line": f.line,
            "severity": f.severity,
            "sink": f.sink,
            "fingerprint": f.fingerprint,
            "status": status,
        })
    new_items = [f.to_dict() for f in current.findings if f.fingerprint not in base_map]
    return {
        "baseline_total": len(baseline.findings),
        "current_total": len(current.findings),
        "fixed_count": fixed,
        "remaining_count": remaining,
        "new_count": len(new_items),
        "new_vulnerabilities": new_items,
        "per_vulnerability": per_vuln,
    }
