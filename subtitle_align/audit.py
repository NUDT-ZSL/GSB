"""逐句核对与汇总。

异常分两个层级：
- **无效句**：时间非法、时间越界、文本缺失。无效句不参与行高比较，
  也不进入预览图时间轴（仅在预览图底部列出原因）。
- **有效句上的标注**：行数不匹配、超行、句内中英时间重叠、与其他有效句
  的时间区间交叠。这些句仍出现在时间轴上，预览图用醒目边框标出。
"""

from __future__ import annotations

from typing import Any

from .textwrap import count_lines, resolve_font

# 句内/句间重叠（红）、行数不匹配（橙）、超行（紫）
FLAG_OVERLAP = "time_overlap"
FLAG_LINE_MISMATCH = "line_mismatch"
FLAG_OVER_LINES = "over_lines"


def _overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    """两个左闭右开区间的重叠毫秒数，不重叠返回 0。"""
    return max(0, min(a_end, b_end) - max(a_start, b_start))


def _sentence_id(sentence: dict[str, Any], index: int) -> str:
    sid = sentence.get("id")
    return sid if isinstance(sid, str) and sid else f"#{index + 1}"


def audit(table: dict[str, Any]) -> dict[str, Any]:
    """对已通过结构校验的字幕表执行全部核对，返回完整报告数据。"""
    config = table["config"]
    canvas_width: int = config["canvas_width"]
    font_size: int = config["font_size"]
    video_duration: int = config["video_duration_ms"]
    max_lines: int = config["max_lines"]
    # 文本左右各留内边距，渲染宽度小于画布宽度
    text_padding = max(8, font_size // 2)
    render_width = canvas_width - text_padding * 2

    font, font_source = resolve_font(font_size)
    sentences = table["subtitles"]

    records: list[dict[str, Any]] = []
    valid_intervals: list[tuple[int, int, int]] = []  # (index, start, end)

    for index, sentence in enumerate(sentences):
        sid = _sentence_id(sentence, index)
        start_ms: int = sentence["start_ms"]
        end_ms: int = sentence["end_ms"]
        zh_text: str = sentence["zh_text"]
        en_text: str = sentence["en_text"]

        record: dict[str, Any] = {
            "id": sid,
            "order": index,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "zh_text": zh_text,
            "en_text": en_text,
            "valid": True,
            "invalid_reasons": [],
            "text_missing": False,
            "zh_missing": False,
            "en_missing": False,
            "time_illegal": False,
            "time_illegal_reason": None,
            "out_of_bounds": False,
            "out_of_bounds_reason": None,
            "zh_lines": None,
            "en_lines": None,
            "line_count_matched": None,
            "line_count_diff": None,
            "over_max_lines": False,
            "over_lines_detail": None,
            "intra_overlap_ms": None,
            "intra_overlap_range": None,
            "overlap_with": [],
            "flags": [],
        }

        # ---- 时间合法性 ----
        if start_ms >= end_ms:
            record["time_illegal"] = True
            record["time_illegal_reason"] = (
                f"开始毫秒 {start_ms} 必须小于结束毫秒 {end_ms}"
            )
            record["invalid_reasons"].append(record["time_illegal_reason"])

        # ---- 时间越界 ----
        oob_parts: list[str] = []
        if start_ms < 0 or end_ms < 0:
            oob_parts.append(f"时间为负（start_ms={start_ms}, end_ms={end_ms}）")
        if start_ms > video_duration or end_ms > video_duration:
            oob_parts.append(
                f"时间超过视频时长 {video_duration}ms"
                f"（start_ms={start_ms}, end_ms={end_ms}）"
            )
        if oob_parts:
            record["out_of_bounds"] = True
            record["out_of_bounds_reason"] = "；".join(oob_parts)
            record["invalid_reasons"].append(record["out_of_bounds_reason"])

        # ---- 文本缺失 ----
        zh_missing = not zh_text.strip()
        en_missing = not en_text.strip()
        record["zh_missing"] = zh_missing
        record["en_missing"] = en_missing
        if zh_missing or en_missing:
            missing_parts = []
            if zh_missing:
                missing_parts.append("中文文本为空或仅空白")
            if en_missing:
                missing_parts.append("英文文本为空或仅空白")
            record["text_missing"] = True
            record["invalid_reasons"].append("；".join(missing_parts))

        # ---- 行数测量与行高比较（文本缺失句不参与） ----
        if not record["text_missing"]:
            zh_lines = count_lines(zh_text, font, render_width)
            en_lines = count_lines(en_text, font, render_width)
            record["zh_lines"] = zh_lines
            record["en_lines"] = en_lines
            record["line_count_matched"] = zh_lines == en_lines
            record["line_count_diff"] = zh_lines - en_lines
            if not record["line_count_matched"]:
                record["flags"].append(FLAG_LINE_MISMATCH)

            over_parts = []
            if zh_lines > max_lines:
                over_parts.append(f"中文 {zh_lines} 行，超出 {zh_lines - max_lines} 行")
            if en_lines > max_lines:
                over_parts.append(f"英文 {en_lines} 行，超出 {en_lines - max_lines} 行")
            if over_parts:
                record["over_max_lines"] = True
                record["over_lines_detail"] = "；".join(over_parts)
                record["flags"].append(FLAG_OVER_LINES)

        # ---- 句内中英时间区间重叠（仅显式提供独立时间时检测） ----
        zh_timing = sentence.get("zh_timing")
        en_timing = sentence.get("en_timing")
        if isinstance(zh_timing, dict) and isinstance(en_timing, dict):
            zs, ze = zh_timing["start_ms"], zh_timing["end_ms"]
            es, ee = en_timing["start_ms"], en_timing["end_ms"]
            intra = _overlap(zs, ze, es, ee)
            if intra > 0:
                record["intra_overlap_ms"] = intra
                record["intra_overlap_range"] = {
                    "overlap_start_ms": max(zs, es),
                    "overlap_end_ms": min(ze, ee),
                }
                record["flags"].append(FLAG_OVERLAP)

        # ---- 有效性判定 ----
        record["valid"] = not (
            record["time_illegal"]
            or record["out_of_bounds"]
            or record["text_missing"]
        )
        if record["valid"]:
            valid_intervals.append((index, start_ms, end_ms))

        records.append(record)

    # ---- 任意两句时间区间交叠（仅统计时间区间本身可用的句） ----
    overlap_pairs: list[dict[str, Any]] = []
    for pos in range(len(valid_intervals)):
        i, i_start, i_end = valid_intervals[pos]
        for j, j_start, j_end in valid_intervals[pos + 1:]:
            amount = _overlap(i_start, i_end, j_start, j_end)
            if amount > 0:
                ov_start = max(i_start, j_start)
                ov_end = min(i_end, j_end)
                pair = {
                    "sentence_ids": [records[i]["id"], records[j]["id"]],
                    "overlap_start_ms": ov_start,
                    "overlap_end_ms": ov_end,
                    "overlap_ms": amount,
                }
                overlap_pairs.append(pair)
                records[i]["overlap_with"].append({
                    "id": records[j]["id"],
                    "overlap_start_ms": ov_start,
                    "overlap_end_ms": ov_end,
                    "overlap_ms": amount,
                })
                records[j]["overlap_with"].append({
                    "id": records[i]["id"],
                    "overlap_start_ms": ov_start,
                    "overlap_end_ms": ov_end,
                    "overlap_ms": amount,
                })
                if FLAG_OVERLAP not in records[i]["flags"]:
                    records[i]["flags"].append(FLAG_OVERLAP)
                if FLAG_OVERLAP not in records[j]["flags"]:
                    records[j]["flags"].append(FLAG_OVERLAP)

    line_mismatch_count = sum(
        1 for r in records if r["line_count_matched"] is False
    )
    invalid_records = [r for r in records if not r["valid"]]

    summary = {
        "total_sentences": len(records),
        "valid_sentences": len(valid_intervals),
        "invalid_sentences": len(invalid_records),
        "line_mismatch_sentences": line_mismatch_count,
        "over_lines_sentences": sum(1 for r in records if r["over_max_lines"]),
        "intra_overlap_sentences": sum(
            1 for r in records if r["intra_overlap_ms"] is not None
        ),
        "overlap_pair_count": len(overlap_pairs),
        "invalid_sentence_ids": [r["id"] for r in invalid_records],
    }

    return {
        "config": {
            **config,
            "text_render_width": render_width,
            "font_source": font_source,
        },
        "summary": summary,
        "sentences": records,
        "overlap_pairs": overlap_pairs,
    }
