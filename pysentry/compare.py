"""修复前后对比：以 (相对路径, 规则标识) 为匹配键，行号变化不视为新问题。"""
from __future__ import annotations

from collections import Counter

from .model import Finding
from .scanner import scan_directory


def _issue_key(finding: Finding) -> tuple[str, str]:
    return finding.file, finding.rule_id


def _multiset_diff(before: Counter, after: Counter) -> tuple[Counter, Counter]:
    """按多重集比较：返回 (已修复键的多重集, 仍存在键的多重集)。

    同一 (文件, 规则) 在前后各出现 n/m 次时：
    min(n, m) 条归入“仍存在”，剩余 max(n-m, 0) 条归入“已修复”。
    """
    fixed: Counter = Counter()
    remaining: Counter = Counter()
    for key, before_count in before.items():
        after_count = after.get(key, 0)
        remaining[key] = min(before_count, after_count)
        if before_count > after_count:
            fixed[key] = before_count - after_count
    return fixed, remaining


def _expand(counter: Counter, pool: list[Finding], pick_from: str) -> list[dict[str, object]]:
    """把多重集结果展开为逐条记录。

    仍存在问题优先采用修复后版本（含新行号）；已修复问题保留修复前行号。
    """
    pool_by_key: dict[tuple[str, str], list[Finding]] = {}
    for finding in pool:
        pool_by_key.setdefault(_issue_key(finding), []).append(finding)

    records: list[dict[str, object]] = []
    for key in sorted(counter):
        count = counter[key]
        source = pool_by_key.get(key, [])
        for idx in range(count):
            finding = source[idx] if idx < len(source) else source[-1]
            records.append({
                "rule_id": finding.rule_id,
                "file": finding.file,
                "line": finding.line,
                "message": finding.message,
                "matched_by": "relative_path+rule_id",
                "line_number_source": pick_from,
            })
    records.sort(key=lambda r: (r["file"], r["line"], r["rule_id"]))
    return records


def compare_scans(before_dir: str, after_dir: str) -> dict[str, object]:
    """对比两个目录的扫描结果。"""
    before_findings, before_skipped, before_files = scan_directory(before_dir)
    after_findings, after_skipped, after_files = scan_directory(after_dir)

    before_counter = Counter(_issue_key(f) for f in before_findings)
    after_counter = Counter(_issue_key(f) for f in after_findings)
    fixed_counter, remaining_counter = _multiset_diff(before_counter, after_counter)

    fixed_records = _expand(fixed_counter, before_findings, "before")
    remaining_records = _expand(remaining_counter, after_findings, "after")

    # 修复后新引入的问题（before 中不存在的键），作为附加信息单列，不混入两类主结果
    introduced_counter = Counter({
        key: max(count - before_counter.get(key, 0), 0)
        for key, count in after_counter.items()
        if count > before_counter.get(key, 0)
    })
    introduced_records = _expand(introduced_counter, after_findings, "after")

    return {
        "before_root": str(before_dir),
        "after_root": str(after_dir),
        "before_issue_count": len(before_findings),
        "after_issue_count": len(after_findings),
        "fixed_count": len(fixed_records),
        "remaining_count": len(remaining_records),
        "fixed_issues": fixed_records,
        "remaining_issues": remaining_records,
        "introduced_issues": introduced_records,
        "before_skipped_files": before_skipped,
        "after_skipped_files": after_skipped,
        "before_scanned_files": before_files,
        "after_scanned_files": after_files,
    }
