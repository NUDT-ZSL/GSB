"""确定性样本生成器：不依赖随机数，重复运行字节一致。

    python samples/generate_samples.py

产出：
    samples/shots_main.json        核心样例，覆盖需求 1–10、12–13
    samples/subtitles_main.json
    samples/shots_overlap.json     单独的重叠冲突样例（需求 7）
    samples/subtitles_overlap.json
    samples/shots_gap.json         单独的空档样例（需求 8）
    samples/subtitles_gap.json
    samples/invalid/*.json         格式非法样例（需求 11）
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
INVALID = HERE / "invalid"


def dump(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {path.relative_to(HERE.parent)}")


# --------------------------------------------------------------------- 主样例
MAIN_SHOTS = {
    "meta": {"name": "main", "note": "乱序输入，四个镜头覆盖 0–9600ms，无重叠无空档"},
    "shots": [
        {"shot_id": "S03", "start_ms": 4800, "end_ms": 7200},
        {"shot_id": "S01", "start_ms": 0, "end_ms": 2400},
        {"shot_id": "S04", "start_ms": 7200, "end_ms": 9600},
        {"shot_id": "S02", "start_ms": 2400, "end_ms": 4800},
    ],
}

MAIN_SUBTITLES = {
    "meta": {"name": "main", "note": "合法 + 各类未完成条目混排，用于验证互不影响"},
    "subtitles": [
        {"text": "第一镜：开场", "start_ms": 300, "end_ms": 1800, "shot_id": "S01"},
        # 横跨 S02→S03 切点（切点在 4800ms），验证切点前后连续
        {"text": "这句字幕横跨切点，不能断", "start_ms": 4200, "end_ms": 5600},
        {"text": "第三镜：特写", "start_ms": 5200, "end_ms": 7000, "shot_id": "S03"},
        # 引用不存在的镜头编号 → unknown_shot
        {"text": "幽灵镜头里的字幕", "start_ms": 6000, "end_ms": 6500, "shot_id": "S99"},
        # 起止倒序 → reversed_range
        {"text": "倒序字幕", "start_ms": 8000, "end_ms": 7500},
        # 超出素材总长度 9600ms → out_of_bounds
        {"text": "越界字幕", "start_ms": 9300, "end_ms": 12000},
        {"text": "结尾一句", "start_ms": 8600, "end_ms": 9400, "shot_id": "S04"},
        # 区间落在镜头内但显式编号的区间不符 → outside_shot
        {"text": "编号与区间不匹配", "start_ms": 900, "end_ms": 2500, "shot_id": "S01"},
        # 文本只有空白 → empty_text
        {"text": "   ", "start_ms": 3000, "end_ms": 3500},
    ],
}

# ------------------------------------------------------------------- 重叠样例
OVERLAP_SHOTS = {
    "meta": {"name": "overlap", "note": "S_B 与 S_A 重叠 800ms，切点应为 S_A 的结束时刻"},
    "shots": [
        {"shot_id": "S_A", "start_ms": 0, "end_ms": 3000},
        {"shot_id": "S_B", "start_ms": 2200, "end_ms": 5000},
        {"shot_id": "S_C", "start_ms": 5000, "end_ms": 7000},
    ],
}

OVERLAP_SUBTITLES = {
    "meta": {"name": "overlap", "note": "跨切点（3000ms）的字幕与引用被重叠镜头的字幕"},
    "subtitles": [
        {"text": "重叠镜头前的字幕", "start_ms": 1000, "end_ms": 2400},
        {"text": "跨过重叠切点的字幕", "start_ms": 2700, "end_ms": 3400},
        {"text": "后一镜头字幕", "start_ms": 3400, "end_ms": 4800, "shot_id": "S_B"},
        # 引用 S_B 且区间落在被切掉的头部（2200–3000ms）→ outside_shot
        {"text": "引用被切掉的头部区间", "start_ms": 2300, "end_ms": 2900, "shot_id": "S_B"},
    ],
}

# ------------------------------------------------------------------- 空档样例
GAP_SHOTS = {
    "meta": {"name": "gap", "note": "S2 结束后 1000ms 空档，再接 S3"},
    "shots": [
        {"shot_id": "S1", "start_ms": 0, "end_ms": 2000},
        {"shot_id": "S2", "start_ms": 2000, "end_ms": 4000},
        {"shot_id": "S3", "start_ms": 5000, "end_ms": 7000},
    ],
}

GAP_SUBTITLES = {
    "meta": {"name": "gap", "note": "空档两侧各一条合法字幕，中间一条跨空档字幕应被拒绝"},
    "subtitles": [
        {"text": "空档之前的字幕", "start_ms": 1500, "end_ms": 2000},
        {"text": "不应出现在空档里的字幕", "start_ms": 3800, "end_ms": 5200},
        {"text": "空档之后的字幕", "start_ms": 5000, "end_ms": 6200},
    ],
}

INVALID_SHOTS = {
    "meta": {"name": "invalid-chars"},
    "shots": [
        {"shot_id": "S01", "start_ms": 0, "end_ms": 2000},
        {"shot_id": "S02", "start_ms": 2000, "end_ms": 4000},
    ],
}
# 故意截断的 JSON
INVALID_SHOTS_BROKEN = '{"meta": {"name": "broken"}, "shots": [ {"shot_id": "S01", "start_ms": 0,'
INVALID_SHOTS_STRUCT = {"meta": {}, "shots": [{"shot_id": "S01", "start_ms": "零", "end_ms": 2000}]}
INVALID_SUBTITLES_STRUCT = {"meta": {}, "subtitles": [{"text": "字幕", "start_ms": 0}]}


def main() -> None:
    dump(HERE / "shots_main.json", MAIN_SHOTS)
    dump(HERE / "subtitles_main.json", MAIN_SUBTITLES)
    dump(HERE / "shots_overlap.json", OVERLAP_SHOTS)
    dump(HERE / "subtitles_overlap.json", OVERLAP_SUBTITLES)
    dump(HERE / "shots_gap.json", GAP_SHOTS)
    dump(HERE / "subtitles_gap.json", GAP_SUBTITLES)

    dump(INVALID / "shots_valid_but_subtitles_wrong_format.json", INVALID_SHOTS)
    dump(
        INVALID / "subtitles_ok.json",
        {"meta": {}, "subtitles": [{"text": "x", "start_ms": 0, "end_ms": 500}]},
    )
    (INVALID / "shots_broken_json.json").write_text(
        INVALID_SHOTS_BROKEN, encoding="utf-8", newline="\n"
    )
    print("wrote samples/invalid/shots_broken_json.json")
    dump(INVALID / "shots_bad_field_type.json", INVALID_SHOTS_STRUCT)
    dump(INVALID / "subtitles_missing_field.json", INVALID_SUBTITLES_STRUCT)


if __name__ == "__main__":
    main()
