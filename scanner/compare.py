"""修复前后报告比对。

以 (sample_id, finding 类别, 证据位置, 字段名) 为同一条暴露的稳定标识，
对比修复前（baseline）与修复后（current）两份报告：
- 已消除：baseline 中存在、current 中消失；
- 未消除：两份报告中均存在（附带当前出现次数）；
- 新增：baseline 中不存在、current 中新出现。

未完成样本不参与条目比对，单独在 incomplete_samples 中列出，避免无结论冒充合规。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .models import (
    CONCLUSION_INCOMPLETE,
    SEVERITY_ORDER,
    STATUS_NEW,
    STATUS_PRESENT,
    STATUS_REMOVED,
)
from .report_io import read_report


def _finding_identity(finding: dict[str, Any]) -> tuple[str, str]:
    """暴露条目的稳定标识（不依赖会随排序变化的 finding_id）。"""

    key = (
        finding["category"],
        finding.get("location") or "",
        finding.get("field_name") or "",
    )
    return finding["severity"], "|".join(key)


def _index(report: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    index: dict[str, dict[str, dict[str, Any]]] = {}
    for sample in report.get("samples", []):
        if sample.get("conclusion") == CONCLUSION_INCOMPLETE:
            continue
        bucket: dict[str, dict[str, Any]] = {}
        for finding in sample.get("findings", []):
            _severity, identity = _finding_identity(finding)
            bucket[identity] = finding
        index[sample["sample_id"]] = bucket
    return index


def compare_reports(baseline_path: Path, current_path: Path) -> dict[str, Any]:
    baseline = read_report(baseline_path)
    current = read_report(current_path)

    base_index = _index(baseline)
    cur_index = _index(current)

    entries: list[dict[str, Any]] = []
    sample_ids = sorted(set(base_index) | set(cur_index))
    for sample_id in sample_ids:
        old = base_index.get(sample_id, {})
        new = cur_index.get(sample_id, {})
        for identity in sorted(set(old) | set(new)):
            old_finding = old.get(identity)
            new_finding = new.get(identity)
            if old_finding and new_finding:
                status = STATUS_PRESENT
                anchor = new_finding
            elif old_finding and not new_finding:
                status = STATUS_REMOVED
                anchor = old_finding
            else:
                status = STATUS_NEW
                anchor = new_finding  # type: ignore[assignment]
            entries.append(
                {
                    "sample_id": sample_id,
                    "status": status,
                    "category": anchor["category"],
                    "severity": anchor["severity"],
                    "conclusion": anchor["conclusion"],
                    "location": anchor["location"],
                    "field_name": anchor.get("field_name"),
                    "occurrences": anchor["occurrences"],
                    "remediation": anchor.get("remediation"),
                }
            )

    entries.sort(
        key=lambda e: (
            e["sample_id"],
            SEVERITY_ORDER[e["severity"]],
            e["category"],
            e["location"],
        )
    )

    incomplete_samples = sorted(
        s["sample_id"]
        for s in current.get("samples", [])
        if s.get("conclusion") == CONCLUSION_INCOMPLETE
    )

    return {
        "baseline_report": str(baseline_path).replace("\\", "/"),
        "current_report": str(current_path).replace("\\", "/"),
        "summary": {
            "total_entries": len(entries),
            "removed": sum(1 for e in entries if e["status"] == STATUS_REMOVED),
            "present": sum(1 for e in entries if e["status"] == STATUS_PRESENT),
            "new": sum(1 for e in entries if e["status"] == STATUS_NEW),
            "incomplete_samples": len(incomplete_samples),
        },
        "incomplete_samples": incomplete_samples,
        "entries": entries,
    }
