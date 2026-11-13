"""两份扫描报告的对比：新增 / 已修复 / 未变化（需求 6）。"""

from __future__ import annotations

import json

from .config import SEVERITY_ORDER


def _entry_key(entry: dict) -> tuple:
    """漏洞身份键：类型 + URL + 参数名（参数名不同视为不同漏洞）。"""
    return entry["type"], entry["url"], entry.get("parameter")


def _all_entries(report: dict) -> list[dict]:
    entries = []
    for target in report.get("targets", []):
        entries.extend(target.get("findings", []))
    return entries


def _sort(entries: list[dict]) -> list[dict]:
    return sorted(entries,
                  key=lambda f: (SEVERITY_ORDER[f["severity"]], f["type"],
                                 f["url"], f.get("parameter") or ""))


def compare_reports(before_path: str, after_path: str) -> dict:
    with open(before_path, "r", encoding="utf-8") as handle:
        before = json.load(handle)
    with open(after_path, "r", encoding="utf-8") as handle:
        after = json.load(handle)

    before_map = {_entry_key(e): e for e in _all_entries(before)}
    after_map = {_entry_key(e): e for e in _all_entries(after)}

    fixed = [before_map[k] for k in before_map.keys() - after_map.keys()]
    added = [after_map[k] for k in after_map.keys() - before_map.keys()]
    unchanged = [after_map[k] for k in before_map.keys() & after_map.keys()]

    return {
        "before_report": before_path,
        "after_report": after_path,
        "counts": {
            "added": len(added),
            "fixed": len(fixed),
            "unchanged": len(unchanged),
        },
        # 每一类明细均按严重性降序
        "added": _sort(added),
        "fixed": _sort(fixed),
        "unchanged": _sort(unchanged),
    }
