"""报告的确定性序列化：同一输入多次生成逐字段相同。"""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Dict

from .model import Report, RoundRecord


def round_to_dict(rec: RoundRecord) -> Dict[str, Any]:
    return asdict(rec)


def report_to_dict(rep: Report) -> Dict[str, Any]:
    return asdict(rep)


def report_to_json(rep: Report) -> str:
    """排序键 + 无 ASCII 转义 + 固定缩进，保证逐字节确定。"""
    return json.dumps(report_to_dict(rep), ensure_ascii=False, indent=2,
                      sort_keys=True) + "\n"


def dump_report(rep: Report, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(report_to_json(rep))


def render_text(rep: Report) -> str:
    """人可读的运营效率报告文本。"""
    lines = []
    lines.append("=== 运营效率报告 ===")
    lines.append(f"整体满意度评分: {rep.overall_score}")
    lines.append(f"体力总消耗: {rep.total_stamina_spent}  补给总消耗: {rep.total_supply_spent}")
    lines.append("")
    lines.append("-- 设施满意度 --")
    for fs in rep.facility_scores:
        if fs.visited:
            d = fs.detail
            lines.append(f"  {fs.facility}: {fs.score} 分（第 {fs.visit_round} 回合到访；"
                         f"基础 {d['base']} + 体力 {d['stamina_points']} "
                         f"+ 补给 {d['supply_points']} + 时长 {d['duration_points']}）")
        else:
            lines.append(f"  {fs.facility}: 未访问（0 分计入整体）")
    if rep.unvisited:
        lines.append(f"未访问目标: {', '.join(rep.unvisited)}")
    lines.append("")
    lines.append("-- 关键告警 --")
    if rep.alerts:
        for a in rep.alerts:
            lines.append(f"  R{a.round_no:02d} @{a.node} [{a.code}] {a.message}（政策: {a.policy}）")
    else:
        lines.append("  无")
    lines.append("")
    lines.append("-- 决策轨迹 --")
    lines.extend(f"  {t}" for t in rep.trajectory)
    return "\n".join(lines) + "\n"
