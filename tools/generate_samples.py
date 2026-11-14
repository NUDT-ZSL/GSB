"""确定性生成全部本地样本字幕表（无外部素材、无随机数）。

运行：python tools/generate_samples.py
输出目录：samples/input/
"""

from __future__ import annotations

import json
from pathlib import Path

CANVAS_WIDTH = 1280
FONT_SIZE = 28
VIDEO_DURATION = 20000
MAX_LINES = 3

# 宽画布下短文本均为 1 行；显式 \n 用于确定性地构造多行
ZH_TWO = "这是第一行中文内容。\n这是第二行中文内容。"
EN_ONE = "This is a single line of English text."
ZH_THREE = "中文第一行。\n中文第二行。\n中文第三行。"
ZH_FOUR = "中文第一行。\n中文第二行。\n中文第三行。\n中文第四行。"
EN_TWO = "English line one here.\nEnglish line two here."


def base_config(video_duration_ms: int = VIDEO_DURATION) -> dict:
    return {
        "canvas_width": CANVAS_WIDTH,
        "font_size": FONT_SIZE,
        "video_duration_ms": video_duration_ms,
        "max_lines": MAX_LINES,
    }


def s(sid, start, end, zh, en, **extra):
    item = {
        "id": sid,
        "start_ms": start,
        "end_ms": end,
        "zh_text": zh,
        "en_text": en,
    }
    item.update(extra)
    return item


def sample_basic() -> dict:
    """10 句正常样本：验证顺序保持与常规核对（需求 1）。"""
    return {
        "config": base_config(),
        "subtitles": [
            s("S01", 0, 1500, "开场字幕第一句。", "Opening subtitle line one."),
            s("S02", 1600, 3000, "第二句中文内容。", "Second sentence in English."),
            s("S03", 3200, 4600, "第三句紧随其后。", "The third one follows."),
            s("S04", 4800, 6200, "第四句没有重叠。", "Fourth, with no overlap."),
            s("S05", 6400, 7800, "第五句时间正常。", "Fifth, timing is fine."),
            s("S06", 8000, 9400, "第六句文本完整。", "Sixth, text complete."),
            s("S07", 9600, 11000, "第七句继续播放。", "Seventh keeps playing."),
            s("S08", 11200, 12600, "第八句间隔均匀。", "Eighth, even spacing."),
            s("S09", 12800, 14200, "第九句接近尾声。", "Ninth, near the end."),
            s("S10", 14400, 16000, "第十句正常收尾。", "Tenth, a clean finish."),
        ],
    }


def sample_line_mismatch() -> dict:
    """中文 2 行 vs 英文 1 行；中文 4 行 vs 英文 2 行（需求 2/13/14）。"""
    return {
        "config": base_config(),
        "subtitles": [
            s("L01", 0, 3000, ZH_TWO, EN_ONE),
            s("L02", 3200, 6000, ZH_THREE, EN_ONE),
            s("L03", 6200, 9000, ZH_FOUR, EN_TWO),
            s("L04", 9200, 12000, "单行中文对照。", "Single line English."),
        ],
    }


def sample_intra_overlap() -> dict:
    """同一句内中文/英文独立时间区间交叉（需求 3）。"""
    return {
        "config": base_config(),
        "subtitles": [
            s(
                "I01", 0, 4000,
                "中文区间与英文区间部分交叉。",
                "Chinese and English ranges cross.",
                zh_timing={"start_ms": 0, "end_ms": 2500},
                en_timing={"start_ms": 1500, "end_ms": 4000},
            ),  # 重叠 1000ms：1500-2500
            s(
                "I02", 4200, 8000,
                "另一句包含区间且完全重叠。",
                "One range fully contains the other.",
                zh_timing={"start_ms": 4200, "end_ms": 8000},
                en_timing={"start_ms": 5000, "end_ms": 6500},
            ),  # 重叠 1500ms：5000-6500
            s(
                "I03", 8200, 12000,
                "两个区间首尾相接不重叠。",
                "Ranges meet but do not overlap.",
                zh_timing={"start_ms": 8200, "end_ms": 10000},
                en_timing={"start_ms": 10000, "end_ms": 12000},
            ),  # 左闭右开，重叠 0
        ],
    }


def sample_cross_overlap() -> dict:
    """三句两两交叠（需求 4）。"""
    return {
        "config": base_config(),
        "subtitles": [
            s("C01", 1000, 5000, "第一句持续到第五秒。", "First runs to five."),
            s("C02", 3000, 7000, "第二句与一三句都重叠。", "Second overlaps both."),
            s("C03", 4500, 9000, "第三句和前两句交叉。", "Third crosses the first two."),
            s("C04", 9500, 12000, "正常句不参与重叠。", "A normal non-overlap line."),
        ],
    }


def sample_time_illegal() -> dict:
    """开始 >= 结束的非法句，同文件其他句不受影响（需求 5）。"""
    return {
        "config": base_config(),
        "subtitles": [
            s("T01", 0, 2000, "正常句排在前面。", "A normal sentence first."),
            s("T02", 5000, 5000, "开始等于结束，非法。", "Start equals end, illegal."),
            s("T03", 8000, 6000, "开始晚于结束，非法。", "Start later than end, illegal."),
            s("T04", 9000, 11000, "其他句照常核对。", "Other sentences checked as usual."),
        ],
    }


def sample_out_of_bounds() -> dict:
    """负值与超出视频时长的条目（需求 6）。"""
    return {
        "config": base_config(video_duration_ms=10000),
        "subtitles": [
            s("B01", -500, 2000, "开始时间为负，越界。", "Negative start, out of bounds."),
            s("B02", 3000, 12000, "结束超出视频时长。", "End exceeds video duration."),
            s("B03", 2500, 4500, "正常句照常核对。", "A normal in-range sentence."),
        ],
    }


def sample_text_missing() -> dict:
    """空中文/空白英文句，不参与行高比较（需求 7）。"""
    return {
        "config": base_config(),
        "subtitles": [
            s("M01", 0, 2500, "", "Chinese text is empty here."),
            s("M02", 2700, 5000, "英文只剩空白字符。", "   "),
            s("M03", 5200, 7600, "中英文都完整的对照句。", "Both texts are complete."),
        ],
    }


def sample_all_invalid() -> dict:
    """全部句时间非法：仍产出可读报告与占位预览图（需求 11）。"""
    return {
        "config": base_config(),
        "subtitles": [
            s("X01", 5000, 4000, "第一句时间倒流。", "First timing reversed."),
            s("X02", 3000, 3000, "第二句零长度。", "Second has zero length."),
            s("X03", 9000, 7000, "第三句同样非法。", "Third is illegal too."),
        ],
    }


def sample_empty() -> dict:
    """字幕数组为空：同样走占位图路径。"""
    return {"config": base_config(), "subtitles": []}


def sample_missing_field() -> dict:
    """删除开始毫秒字段：工具必须报错停止且不生成预览图（需求 10）。"""
    broken = s("F01", 0, 2000, "缺少开始毫秒字段。", "Missing start_ms field.")
    del broken["start_ms"]
    return {"config": base_config(), "subtitles": [broken]}


def sample_bad_canvas() -> dict:
    """全局画布宽度非法（0）：明确错误并停止（需求 10）。"""
    cfg = base_config()
    cfg["canvas_width"] = 0
    return {
        "config": cfg,
        "subtitles": [s("G01", 0, 2000, "画布宽度为零。", "Canvas width is zero.")],
    }


SAMPLES = {
    "basic": sample_basic,
    "line_mismatch": sample_line_mismatch,
    "intra_overlap": sample_intra_overlap,
    "cross_overlap": sample_cross_overlap,
    "time_illegal": sample_time_illegal,
    "out_of_bounds": sample_out_of_bounds,
    "text_missing": sample_text_missing,
    "all_invalid": sample_all_invalid,
    "empty": sample_empty,
    "missing_field": sample_missing_field,
    "bad_canvas": sample_bad_canvas,
}


def main() -> None:
    out_dir = Path(__file__).resolve().parent.parent / "samples" / "input"
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, builder in SAMPLES.items():
        target = out_dir / f"{name}.json"
        target.write_text(
            json.dumps(builder(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(f"generated {target}")


if __name__ == "__main__":
    main()
