# -*- coding: utf-8 -*-
"""确定性生成字幕清单与换算参数样本。

固定随机种子，任意机器上重跑产出完全一致，便于核对与重建。

用法：
    python tools/generate_sample.py --out-dir samples
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

SEED = 20261115


def generate() -> tuple[dict, dict]:
    rng = random.Random(SEED)

    entries: list[dict] = []

    # 1) 正常字幕：两条 seconds、一条 milliseconds、一条源 frames
    entries.append({"track": 1, "text": "开场白：各位好", "start": 1.0, "end": 3.5, "unit": "seconds"})
    entries.append({"track": 1, "text": "介绍本期主题", "start": 4.0, "end": 6.0, "unit": "seconds"})
    entries.append({"track": 2, "text": "（背景音乐渐起）", "start": 1500, "end": 3200, "unit": "milliseconds"})
    entries.append({"track": 2, "text": "（掌声）", "start": 180, "end": 210, "unit": "frames"})

    # 2) 同轨重叠：两条均在轨道 1，时间相交
    entries.append({"track": 1, "text": "嘉宾甲发言", "start": 10.0, "end": 13.0, "unit": "seconds"})
    entries.append({"track": 1, "text": "嘉宾乙插话", "start": 12.0, "end": 15.0, "unit": "seconds"})

    # 3) 异轨同时间：轨道 3 与轨道 4 完全重叠，允许
    entries.append({"track": 3, "text": "中文字幕：欢迎收看", "start": 20.0, "end": 24.0, "unit": "seconds"})
    entries.append({"track": 4, "text": "English: Welcome", "start": 20.0, "end": 24.0, "unit": "seconds"})

    # 4) 越过片尾（片尾 60s）：保留原文案并标注
    entries.append({"track": 1, "text": "片尾鸣谢名单（超长不被截断）", "start": 58.0, "end": 65.0, "unit": "seconds"})

    # 5) 字段非法：缺 start/end
    entries.append({"track": 1, "text": "这条缺起止时间", "unit": "seconds"})

    # 6) 起止倒置
    entries.append({"track": 2, "text": "倒置条目", "start": 40.0, "end": 38.0, "unit": "seconds"})

    # 7) 随机填充若干正常条目，打乱输入顺序（总量 <= 300）
    for _ in range(12):
        track = rng.randint(1, 4)
        start = round(rng.uniform(25.0, 55.0), 2)
        end = round(start + rng.uniform(0.5, 3.0), 2)
        entries.append(
            {
                "track": track,
                "text": f"随机字幕 {track}-{start}",
                "start": start,
                "end": end,
                "unit": "seconds",
            }
        )
    rng.shuffle(entries)

    manifest = {"entries": entries}
    params = {
        "target_fps": 25,
        "source_fps": 30,
        "head_seconds": 0.0,
        "tail_seconds": 60.0,
    }
    return manifest, params


def main() -> int:
    parser = argparse.ArgumentParser(description="确定性生成样本清单与参数")
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()

    manifest, params = generate()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.out_dir / "params.json").write_text(
        json.dumps(params, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"样本已生成: {args.out_dir}（{len(manifest['entries'])} 条，种子 {SEED}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
