"""汇总文件渲染（需求 5、8、11）。

输出单个 Markdown 汇总：完成技能计数与逐音效明细、跳过名单、未完成名单。
内容不含运行时间戳，保证重复运行结果逐字节一致。
"""
from __future__ import annotations

from .model import FIT_LABELS
from .pipeline import RunResult, format_num

SUMMARY_FILENAME = "summary.md"


def build_summary(result: RunResult) -> str:
    lines: list[str] = []
    lines.append("# 技能音效检视卡汇总")
    lines.append("")
    lines.append("## 总览")
    lines.append("")
    lines.append(f"- 完成技能数：{len(result.completed)}")
    lines.append(f"- 跳过技能数（字段缺失/类型不符）：{len(result.skipped)}")
    lines.append(f"- 未完成技能数（总时长或画布非正）：{len(result.unfinished)}")
    lines.append(f"- 音效总数：{sum(c.total_sounds for c in result.completed)}")
    lines.append(f"- 越界音效总数：{result.total_out_of_bounds}")
    lines.append("")

    # ---- 完成名单 ----
    lines.append("## 完成技能（检视卡按技能名升序）")
    lines.append("")
    if not result.completed:
        lines.append("（无）")
        lines.append("")
    else:
        lines.append("| 序号 | 页名/文件名 | 总时长(ms) | 画布 | 音效总数 | 越界数 | 检视卡文件 |")
        lines.append("|---|---|---|---|---|---|---|")
        for skill in result.completed:
            lines.append(
                f"| {skill.position} | {skill.page_name} "
                f"| {format_num(skill.duration_ms, 1)} "
                f"| {skill.canvas_w}×{skill.canvas_h} "
                f"| {skill.total_sounds} | {skill.out_of_bounds_count} "
                f"| {skill.filename} |"
            )
        lines.append("")

        # 逐技能音效明细，供逐页核对（需求 5：汇总越界数与各页一致）
        for skill in result.completed:
            lines.append(f"### {skill.page_name}（清单位置 {skill.position}）")
            lines.append("")
            lines.append(
                f"总时长 {format_num(skill.duration_ms, 1)} ms；"
                f"音效 {skill.total_sounds} 个，越界 {skill.out_of_bounds_count} 个"
            )
            lines.append("")
            lines.append("| 音效 | 时刻(ms) | 缩放 | 中心点(x,y) | 与最近关键帧间隔(ms) | 贴合/越界 |")
            lines.append("|---|---|---|---|---|---|")
            for snd in skill.sounds:
                if snd.out_of_bounds:
                    lines.append(
                        f"| {snd.label} | {format_num(snd.t, 1)} | — | — | — "
                        f"| 越界（超出 0~{format_num(skill.duration_ms, 1)}ms，不参与贴合度） |"
                    )
                else:
                    lines.append(
                        f"| {snd.label} | {format_num(snd.t, 1)} "
                        f"| {format_num(snd.zoom, 3)} "
                        f"| ({format_num(snd.cx, 2)}, {format_num(snd.cy, 2)}) "
                        f"| {format_num(snd.gap_ms, 1)} "
                        f"| {FIT_LABELS[snd.fit]}（最近帧 {format_num(snd.nearest_kf_ms, 1)}ms） |"
                    )
            lines.append("")

    # ---- 跳过名单 ----
    lines.append("## 跳过名单（字段缺失或类型不符，未生成检视卡）")
    lines.append("")
    if not result.skipped:
        lines.append("（无）")
    else:
        for item in result.skipped:
            lines.append(f"- 位置 {item.position} · {item.page_name}：{item.reason}")
    lines.append("")

    # ---- 未完成名单 ----
    lines.append("## 未完成名单（总时长或画布尺寸不大于 0，未生成检视卡）")
    lines.append("")
    if not result.unfinished:
        lines.append("（无）")
    else:
        for item in result.unfinished:
            lines.append(f"- 位置 {item.position} · {item.page_name}：{item.reason}")
    lines.append("")
    return "\n".join(lines)


def write_summary(result: RunResult, out_dir: str) -> str:
    path = f"{out_dir}/{SUMMARY_FILENAME}".replace("\\", "/")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(build_summary(result))
    return path
