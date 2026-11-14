"""确定性业务样例生成器。

不使用任何随机源：所有镜头与字幕数据为固定业务样例，重复运行生成字节一致的 JSON。
覆盖：有效、越界（早于起点/晚于终点/双向越界/跨镜头）、重叠、起止帧无效、
      镜头乱序、镜头重叠剔除、缺失字段、帧类型错误。

用法：
    python samples/generate_samples.py            # 写入 samples/data/
"""

from __future__ import annotations

import json
from pathlib import Path

# 帧率 25fps：1 帧 = 40ms
# 4 个正常镜头（半开区间），S1 起点故意留 5 帧前导，用于演示早于起点越界
SHOTS = [
    {"shot_id": "S1", "start_frame": 5,   "end_frame": 250},
    {"shot_id": "S2", "start_frame": 250, "end_frame": 500},
    {"shot_id": "S3", "start_frame": 500, "end_frame": 750},
    {"shot_id": "S4", "start_frame": 750, "end_frame": 1000},
]

# 故意打乱输入顺序，验证需求 9 的排序不受输入顺序影响
SUBTITLES = [
    {"id": "T12", "text": "我们回头再说",       "start_frame": 900, "end_frame": 960},
    {"id": "T02", "text": "你确定吗",           "start_frame": 100, "end_frame": 160},
    {"id": "T10", "text": "重叠字幕甲",         "start_frame": 600, "end_frame": 660},
    {"id": "T07", "text": "结束帧早于开始帧",   "start_frame": 120, "end_frame": 90},
    {"id": "T01", "text": "今晚的月色真美",     "start_frame": 10,  "end_frame": 80},
    {"id": "T11", "text": "重叠字幕乙",         "start_frame": 640, "end_frame": 700},
    {"id": "T08", "text": "镜头边界贴合",       "start_frame": 250, "end_frame": 300},
    {"id": "T04", "text": "晚于终点的字幕",     "start_frame": 200, "end_frame": 260},
    {"id": "T03", "text": "早于起点的字幕",     "start_frame": 3,   "end_frame": 60},
    {"id": "T09", "text": "跨镜头字幕",         "start_frame": 488, "end_frame": 508},
    {"id": "T06", "text": "起止帧相同",         "start_frame": 30,  "end_frame": 30},
    {"id": "T05", "text": "双向越界的字幕",     "start_frame": 247, "end_frame": 505},
]

# 镜头冲突样例：乱序输入 + X2 与 S2 区间重叠（仅 3 个镜头，剔除后空缺区间可观察）
SHOTS_CONFLICT = [
    {"shot_id": "S2", "start_frame": 250, "end_frame": 500},
    {"shot_id": "S1", "start_frame": 0,   "end_frame": 250},
    {"shot_id": "X2", "start_frame": 300, "end_frame": 450},
]
SUBTITLES_CONFLICT = [
    {"id": "C01", "text": "正常镜头内字幕",   "start_frame": 100, "end_frame": 180},
    # X2[300,450) 被剔除后，该字幕在保留镜头 S2[250,500) 内仍可归属 -> 验证“继续处理”
    {"id": "C02", "text": "落在被剔除镜头里", "start_frame": 320, "end_frame": 400},
    # 越过保留镜头终点：X2 已剔除，无人能容纳 -> 越界
    {"id": "C03", "text": "剔除后无家可归",   "start_frame": 480, "end_frame": 540},
]

# 错误样例 1：缺少 start_frame 键
SUBTITLES_MISSING_KEY = [
    {"id": "E01", "text": "缺少起始帧", "end_frame": 100},
]

# 错误样例 2：帧字段类型错误（字符串而非整数）
SUBTITLES_BAD_TYPE = [
    {"id": "E02", "text": "帧号写成字符串", "start_frame": "10", "end_frame": 100},
]


def _dump(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    base = Path(__file__).resolve().parent / "data"

    _dump(base / "shots.json", {"fps": 25, "shots": SHOTS})
    _dump(base / "subtitles.json", {"subtitles": SUBTITLES})

    _dump(base / "shots_conflict.json", {"fps": 25, "shots": SHOTS_CONFLICT})
    _dump(base / "subtitles_conflict.json", {"subtitles": SUBTITLES_CONFLICT})

    _dump(base / "subtitles_missing_key.json", {"subtitles": SUBTITLES_MISSING_KEY})
    _dump(base / "subtitles_bad_type.json", {"subtitles": SUBTITLES_BAD_TYPE})

    print(f"已生成 6 个样例清单到 {base}")


if __name__ == "__main__":
    main()
