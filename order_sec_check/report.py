"""结构化报告读写。

报告分为两类：
1. detection 检测报告：每条用例含 case_id/type/hit/status/request_summary/
   response_summary/reject_reasons/error，按用例编号升序，摘要缺失为 null；
   并区分 completed 与 incomplete 两部分。
2. comparison 修复对比报告：对同一批用例比较修复前后结论
   （fixed/unfixed/functional_diff/...），并包含正常订单查询的功能回退判据。

同名文件永不覆盖：文件名带时间戳 + 递增序号，确保重复运行时旧报告原样保留。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


def _sort_key(entry: dict[str, Any]) -> tuple[int, str]:
    # 用例编号升序：优先按编号中的数字部分，无法解析时回退为字典序
    cid = str(entry.get("case_id", ""))
    digits = "".join(ch for ch in cid if ch.isdigit())
    return (int(digits) if digits else 10**9, cid)


def sort_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(entries, key=_sort_key)


def unique_report_path(directory: Path, prefix: str) -> Path:
    """生成不与现有文件冲突的报告路径。"""
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for seq in range(1, 1000):
        candidate = directory / f"{prefix}_{stamp}_{seq:02d}.json"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"报告目录 {directory} 中同名报告过多，无法生成新文件名")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    # 新路径已保证不存在；exist_ok=False 再兜底一次，绝不覆盖
    fd = path.open("x", encoding="utf-8")
    try:
        json.dump(payload, fd, ensure_ascii=False, indent=2)
        fd.write("\n")
    finally:
        fd.close()


def build_detection_report(
    *,
    mode: str,
    target_base_url: str,
    case_file: str,
    entries: list[dict[str, Any]],
    resumed_from: str | None = None,
) -> dict[str, Any]:
    ordered = sort_entries(entries)
    completed = [e["case_id"] for e in ordered if e["status"] != "incomplete"]
    incomplete = [
        {
            "case_id": e["case_id"],
            "type": e["type"],
            "reason": e.get("error"),
            "retry_hint": "目标暂时不可达，可使用 --resume 对该批用例再次检测",
        }
        for e in ordered
        if e["status"] == "incomplete"
    ]
    return {
        "report_type": "detection",
        "mode": mode,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "target": {"base_url": target_base_url},
        "case_file": str(case_file),
        "resumed_from": resumed_from,
        "summary": {
            "total": len(ordered),
            "completed": len(completed),
            "incomplete": len(incomplete),
            "hit": sum(1 for e in ordered if e["status"] == "hit"),
            "miss": sum(1 for e in ordered if e["status"] == "miss"),
            "rejected": sum(1 for e in ordered if e["status"] == "rejected"),
        },
        "completed_case_ids": completed,
        "incomplete_cases": incomplete,
        "results": ordered,
    }


def build_comparison_report(
    *,
    target_base_url: str,
    case_file: str,
    baseline_report_path: str,
    entries: list[dict[str, Any]],
    functional_checks: list[dict[str, Any]],
) -> dict[str, Any]:
    ordered = sort_entries(entries)
    return {
        "report_type": "comparison",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "target": {"base_url": target_base_url},
        "case_file": str(case_file),
        "baseline_report": baseline_report_path,
        "summary": {
            "total": len(ordered),
            "fixed": sum(1 for e in ordered if e["verdict"] == "fixed"),
            "unfixed": sum(1 for e in ordered if e["verdict"] == "unfixed"),
            "still_clean": sum(1 for e in ordered if e["verdict"] == "still_clean"),
            "rejected": sum(1 for e in ordered if e["verdict"] == "rejected"),
            "incomplete": sum(1 for e in ordered if e["verdict"] == "incomplete"),
            "functional_abnormal": sum(
                1 for f in functional_checks if f["regression"] is True
            ),
        },
        "functional_regression_checks": functional_checks,
        "results": ordered,
    }


def load_report(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
