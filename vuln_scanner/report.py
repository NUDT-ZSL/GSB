"""JSON 报告生成与修复版本基线对比。"""

from __future__ import annotations

import json
from pathlib import Path

from .scanner import ScanResult, utc_now_iso

STATUS_FIXED = "fixed"
STATUS_OPEN = "open"
STATUS_NEW = "new"


def build_report(result: ScanResult) -> dict:
    """把扫描结果组装为报告 dict（发现项已在扫描器中排序）。"""
    counts = {"high": 0, "medium": 0, "low": 0}
    for finding in result.findings:
        counts[finding.risk] = counts.get(finding.risk, 0) + 1
    return {
        "scan_info": {
            "scanner_version": _scanner_version(),
            "generated_at": utc_now_iso(),
            "input_root": result.root,
            "security_log": result.log_path,
        },
        "summary": {
            "total_findings": len(result.findings),
            "high": counts["high"],
            "medium": counts["medium"],
            "low": counts["low"],
            "parsed_files": len(result.parsed_files),
            "parse_failed": len(result.parse_failures),
            "rejected_or_skipped": len(result.rejected_entries),
        },
        "findings": [f.to_dict() for f in result.findings],
        "parsed_files": result.parsed_files,
        "parse_failures": result.parse_failures,
        "rejected_entries": result.rejected_entries,
        "config_issues": result.config_issues,
    }


def write_report(report: dict, output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def compare_with_baseline(current: ScanResult, baseline_report: dict) -> dict:
    """以基线报告为参照，标识当前扫描中各项的修复状态。

    匹配键为 (文件名, 字段路径, 规则名)：
    - 基线和当前都存在 -> open（仍存在）
    - 基线存在、当前消失 -> fixed（已修复）
    - 基线没有、当前出现 -> new（新增）
    原始字段值由扫描器只读保证，不在此流程中做任何改写。
    """
    baseline_items = {
        (item["file_name"], item["field_path"], item["rule"]): item
        for item in baseline_report.get("findings", [])
    }
    current_items = {finding.key(): finding for finding in current.findings}

    comparison: list[dict] = []
    for key in sorted(set(baseline_items) | set(current_items)):
        if key in baseline_items and key in current_items:
            status, source = STATUS_OPEN, current_items[key].to_dict()
        elif key in baseline_items:
            status, source = STATUS_FIXED, baseline_items[key]
        else:
            status, source = STATUS_NEW, current_items[key].to_dict()
        entry = dict(source)
        entry["status"] = status
        comparison.append(entry)

    fixed_count = sum(1 for e in comparison if e["status"] == STATUS_FIXED)
    open_count = sum(1 for e in comparison if e["status"] == STATUS_OPEN)
    new_count = sum(1 for e in comparison if e["status"] == STATUS_NEW)
    return {
        "baseline_generated_at": baseline_report.get("scan_info", {}).get("generated_at"),
        "current_generated_at": utc_now_iso(),
        "summary": {
            "baseline_total": len(baseline_items),
            "current_total": len(current_items),
            "fixed": fixed_count,
            "still_open": open_count,
            "new": new_count,
        },
        "comparison": comparison,
    }


def _scanner_version() -> str:
    from . import __version__

    return __version__
