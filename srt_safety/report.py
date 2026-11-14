"""汇总报告生成：机器可读 JSON + 人工可读 Markdown。"""
from __future__ import annotations

import json
from pathlib import Path

from .audit import CueRecord
from .config import CanvasConfig


def _record_to_dict(rec: CueRecord) -> dict:
    return {
        "index": rec.index,
        "time_text": rec.time_text,
        "text": rec.text,
        "start_ms": rec.start_ms,
        "end_ms": rec.end_ms,
        "frame_range": {"start": rec.start_frame, "end": rec.end_frame},
        "inverted": rec.inverted,
        "line_count": rec.line_count,
        "lines": rec.lines,
        "max_width_px": rec.max_width_px,
        "block_height_px": rec.block_height_px,
        "exceeds_safe_width": rec.exceeds_safe_width,
        "lane": rec.lane,
        "rect": rec.rect,
        "margins_px": rec.margins,
        "outside_safe": rec.outside_safe,
        "overflow_directions": rec.overflow_directions,
        "conflict_flags": rec.conflict_flags,
        "overlap_partners": rec.overlap_partners,
    }


def write_reports(out_dir: str | Path, cfg: CanvasConfig, safe_box: dict,
                  records: list[CueRecord], parse_errors: list,
                  overlap_pairs: list[tuple[int, int]],
                  inverted_indexes: list[int], total_blocks: int,
                  anomalous_count: int, padding: int,
                  srt_path: str, config_path: str) -> None:
    out = Path(out_dir)
    summary = {
        "total_blocks": total_blocks,
        "valid_cues": len(records),
        "skipped_blocks": len(parse_errors),
        "inverted_cues": len(inverted_indexes),
        "overlap_pairs": len(overlap_pairs),
        "out_of_safe_cues": sum(1 for r in records if r.outside_safe),
        "anomalous_records": anomalous_count,
    }

    payload = {
        "inputs": {"srt": str(srt_path), "config": str(config_path)},
        "config": {
            "width": cfg.width,
            "height": cfg.height,
            "fps": cfg.fps,
            "safe_ratio_x": cfg.safe_ratio_x,
            "safe_ratio_y": cfg.safe_ratio_y,
            "font_size": cfg.font_size,
            "line_height": cfg.line_height,
            "safe_box_px": safe_box,
        },
        "summary": summary,
        "conflicts": {
            "inverted_indexes": inverted_indexes,
            "overlap_pairs": [{"a": a, "b": b} for a, b in overlap_pairs],
        },
        "parse_errors": [
            {"block_no": e.block_no, "reason": e.reason, "raw": e.raw}
            for e in parse_errors
        ],
        "records": [_record_to_dict(r) for r in records],
    }
    (out / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines: list[str] = []
    lines.append("# 字幕安全区审计报告")
    lines.append("")
    lines.append(f"- SRT 输入：`{srt_path}`")
    lines.append(f"- 配置输入：`{config_path}`")
    lines.append(
        f"- 画幅：{cfg.width}×{cfg.height}px，帧率 {cfg.fps:g}fps；"
        f"字号 {cfg.font_size}px，行高 {cfg.line_height}px"
    )
    lines.append(
        f"- 安全区（黄框）像素矩形：左 {safe_box['x0']}，上 {safe_box['y0']}，"
        f"右 {safe_box['x1']}，下 {safe_box['y1']}，"
        f"可用宽 {safe_box['width']}px，高 {safe_box['height']}px"
    )
    lines.append("")
    lines.append("## 统计")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("|---|---|")
    labels = {
        "total_blocks": "SRT 总块数",
        "valid_cues": "有效字幕条数",
        "skipped_blocks": "坏块跳过条数",
        "inverted_cues": "帧倒置条数",
        "overlap_pairs": "帧区间交叠对数",
        "out_of_safe_cues": "越出安全区条数",
        "anomalous_records": "异常记录数（并集）",
    }
    for key, label in labels.items():
        lines.append(f"| {label} | {summary[key]} |")
    lines.append("")

    lines.append("## 冲突清单")
    lines.append("")
    if inverted_indexes:
        lines.append("### 帧倒置（结束帧早于起始帧）")
        lines.append("")
        for idx in inverted_indexes:
            lines.append(f"- 序号 {idx}：原因=倒置")
        lines.append("")
    if overlap_pairs:
        lines.append("### 帧区间交叠")
        lines.append("")
        for a, b in overlap_pairs:
            lines.append(f"- 序号对 ({a}, {b})：帧区间相交，已按开始帧错位排布")
        lines.append("")
    if not inverted_indexes and not overlap_pairs:
        lines.append("无帧区间冲突。")
        lines.append("")
    if parse_errors:
        lines.append("### 坏块跳过（SRT 解析错误）")
        lines.append("")
        for err in parse_errors:
            lines.append(f"- 物理块 {err.block_no}：{err.reason}")
        lines.append("")

    lines.append("## 逐条记录")
    lines.append("")
    for rec in records:
        png_name = f"{rec.index:0{padding}d}.png"
        lines.append(f"### 序号 {rec.index}（代表帧 `{png_name}`）")
        lines.append("")
        lines.append(f"- 原始时间文本：`{rec.time_text}`")
        lines.append(f"- 正文：{rec.text.replace(chr(10), ' / ')}")
        lines.append(
            f"- 帧区间：[{rec.start_frame}, {rec.end_frame})"
            f"（毫秒 {rec.start_ms} → {rec.end_ms}）"
        )
        lines.append(
            f"- 换行行数：{rec.line_count}；各行像素宽度："
            + "，".join(f"{ln['width_px']}px" for ln in rec.lines)
        )
        lines.append(
            f"- 四向边距(px)：左 {rec.margins['left']}，右 {rec.margins['right']}，"
            f"上 {rec.margins['top']}，下 {rec.margins['bottom']}"
        )
        lines.append(
            f"- 安全区判定：{'越界（' + '、'.join(rec.overflow_directions) + '）' if rec.outside_safe else '在安全区内'}"
        )
        lines.append(
            f"- 错位泳道：{rec.lane if rec.lane is not None else '不参与（倒置）'}"
        )
        lines.append(
            f"- 冲突标记：{('、'.join(rec.conflict_flags)) if rec.conflict_flags else '无'}"
            + (f"；交叠对手序号 {rec.overlap_partners}" if rec.overlap_partners else "")
        )
        lines.append("")

    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
