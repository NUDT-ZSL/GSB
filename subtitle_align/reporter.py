"""核对报告序列化与中文汇总输出。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_report(report: dict[str, Any], path: str | Path) -> None:
    """以固定格式写入报告 JSON（ensure_ascii=False、键序固定、换行 \\n）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # sort_keys 固定键序 + 固定分隔符/缩进，保证重复运行字节一致
    payload = json.dumps(
        report,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        separators=(",", ": "),
    )
    path.write_text(payload + "\n", encoding="utf-8", newline="\n")


def format_summary(report: dict[str, Any]) -> str:
    """生成打印到终端的中文汇总（需求 15）。"""
    s = report["summary"]
    lines = [
        "=" * 56,
        "双语字幕核对汇总",
        "=" * 56,
        f"总句数：{s['total_sentences']}",
        f"有效句数：{s['valid_sentences']}",
        f"无效句数：{s['invalid_sentences']}",
        f"行数不匹配句数：{s['line_mismatch_sentences']}",
        f"超行句数：{s['over_lines_sentences']}",
        f"句内中英时间重叠句数：{s['intra_overlap_sentences']}",
        f"句间时间重叠句对数：{s['overlap_pair_count']}",
    ]
    if s["invalid_sentence_ids"]:
        lines.append("无效句清单：" + "、".join(s["invalid_sentence_ids"]))
        for record in report["sentences"]:
            if not record["valid"]:
                lines.append(f"  - {record['id']}：" + "；".join(record["invalid_reasons"]))
    else:
        lines.append("无效句清单：无")

    if report["overlap_pairs"]:
        lines.append("句间交叠句对：")
        for pair in report["overlap_pairs"]:
            a, b = pair["sentence_ids"]
            lines.append(
                f"  - {a} <-> {b}：{pair['overlap_start_ms']}ms-"
                f"{pair['overlap_end_ms']}ms（{pair['overlap_ms']}ms）"
            )
    lines.append("=" * 56)
    return "\n".join(lines)
