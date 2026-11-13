"""JSONL 扫描记录的查询入口（需求 12、13）。

记录文件每行一个 JSON 对象，字段与报告条目一致：
target_url / vulnerability_type(_label) / url / parameter / severity / remediation
"""

from __future__ import annotations

import json

from .config import SEVERITY_ORDER, ALL_CHECKS


def load_records(path: str) -> list[dict]:
    records = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))  # 逐行可解析（需求 12）
    return records


def query_records(path: str, *, url_prefix: str | None = None,
                  severity: str | None = None) -> list[dict]:
    """按目标 URL 前缀与严重性过滤，结果按严重性降序排列（需求 13）。"""
    if severity is not None and severity not in ("high", "medium", "low"):
        raise ValueError("severity 必须是 high/medium/low")
    if url_prefix is not None and not (
            url_prefix.startswith("http://") or url_prefix.startswith("https://")):
        raise ValueError("url_prefix 必须是 http/https 前缀")

    matched = []
    for record in load_records(path):
        if url_prefix and not record["target_url"].startswith(url_prefix):
            continue
        if severity and record["severity"] != severity:
            continue
        matched.append(record)

    matched.sort(key=lambda r: (SEVERITY_ORDER[r["severity"]],
                                r["vulnerability_type"], r["url"],
                                r.get("parameter") or ""))
    return matched
