"""交接报告渲染：JSON 为机器可读主报告，Markdown 为人工交接视图。

报告组织（需求 8）：
- 按文件分组，文件顺序按文件名固定排序；
- 组内主键按字典序固定排序；
- 结论只有“完整可用”和“未完成”两类。
"""

from __future__ import annotations

import json

from .pipeline import atomic_write_text


def render_markdown(report: dict) -> str:
    lines: list[str] = []
    s = report["summary"]
    lines.append(f"# 业务记录交接报告（{report['batch_id']}）")
    lines.append("")
    lines.append(f"- 生成时间：{report['generated_at']}")
    lines.append(f"- 输入目录：`{report['input_dir']}`")
    lines.append(f"- 输出目录：`{report['output_dir']}`")
    lines.append("")
    lines.append("## 汇总")
    lines.append("")
    lines.append(f"- 文件总数：{s['files_total']}（完整可用 {s['files_complete']} / "
                 f"未完成 {s['files_unfinished']}）")
    lines.append(f"- 摄入有效记录：{s['input_records_accepted']} 条")
    lines.append(f"- 缺必填被拒绝：{s['records_rejected_missing_required']} 条")
    lines.append(f"- 规范记录（去重后）：{s['canonical_records']} 条")
    lines.append(f"- 重复主键合并事件：{s['duplicate_merge_events']} 起")
    lines.append(f"- 来源字段冲突裁决：{s['field_conflict_events']} 起")
    lines.append("")

    lines.append("## 结论一：完整可用")
    lines.append("")
    if report["conclusion"]["complete_usable"]:
        for name in report["conclusion"]["complete_usable"]:
            lines.append(f"- `{name}`")
    else:
        lines.append("- （无）")
    lines.append("")

    lines.append("## 结论二：未完成（需要跟进）")
    lines.append("")
    if report["conclusion"]["unfinished"]:
        for name in report["conclusion"]["unfinished"]:
            lines.append(f"- `{name}`")
    else:
        lines.append("- （无）")
    lines.append("")

    lines.append("## 文件明细（按文件名排序）")
    lines.append("")
    for group in report["file_groups"]:
        status_text = "完整可用" if group["file"] in report["conclusion"]["complete_usable"] \
            else "未完成"
        lines.append(f"### `{group['file']}` — {status_text}")
        lines.append("")
        lines.append(f"- 来源：{group['source'] or '（未匹配来源）'}；"
                     f"状态：`{group['status']}`；大小：{group['size_bytes']} 字节")
        if group["detail"]:
            lines.append(f"- 说明：{group['detail']}")
        issue = group.get("read_issue")
        if issue:
            lines.append(f"- 本次读取异常：`{issue['status']}` — {issue['detail']}")
        if group["bad_line_no"]:
            lines.append(f"- 异常行：第 {group['bad_line_no']} 行，"
                         f"内容预览：`{group['bad_line_preview']}`")
        lines.append("")

        lines.append("#### 进入规范记录集的主键（排序）")
        lines.append("")
        if group["accepted_pks"]:
            for pk in group["accepted_pks"]:
                lines.append(f"- {pk}")
        else:
            lines.append("- （无）")
        lines.append("")

        lines.append("#### 缺必填字段被拒绝的记录（主键 + 缺失项）")
        lines.append("")
        if group["rejections"]:
            for rj in group["rejections"]:
                lines.append(
                    f"- 主键 `{rj['pk']}`（第 {rj['line']} 行），"
                    f"缺失字段：{', '.join(rj['missing_fields'])}"
                )
        else:
            lines.append("- （无）")
        lines.append("")

        lines.append("#### 重复主键合并事件")
        lines.append("")
        if group["duplicate_merges"]:
            for ev in group["duplicate_merges"]:
                w = ev["winner"]
                losers = "；".join(
                    f"{x['source']}/{x['file']}:L{x['line']}" for x in ev["losers"]
                )
                lines.append(
                    f"- 主键 `{ev['pk']}`：保留 "
                    f"{w['source']}/{w['file']}:L{w['line']}"
                    f"（业务时间 {w['business_time']}），淘汰 {losers}"
                )
        else:
            lines.append("- （无）")
        lines.append("")

        lines.append("#### 来源字段冲突裁决")
        lines.append("")
        if group["field_conflicts"]:
            for ev in group["field_conflicts"]:
                vals = "；".join(
                    f"{v['source']}(优先级{v['priority']})={json.dumps(v['value'], ensure_ascii=False)}"
                    for v in ev["values"]
                )
                lines.append(
                    f"- 主键 `{ev['pk']}` 字段 `{ev['field']}`：{vals}"
                    f" -> 裁决取 {ev['resolved_to']['source']} = "
                    f"{json.dumps(ev['resolved_to']['value'], ensure_ascii=False)}"
                )
        else:
            lines.append("- （无）")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_reports(report: dict, json_path, md_path) -> None:
    atomic_write_text(
        json_path,
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
    )
    atomic_write_text(md_path, render_markdown(report))
