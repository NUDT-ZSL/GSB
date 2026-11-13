#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成演示用的确定性 WAV 与 JSON 标注（仅标准库）。

运行：
    python -I demo\\make_demo_data.py
产出（均在 demo/data 下）：
    target.wav / target_annotations.json   目标：8000Hz、1.000s
    source.wav / source_annotations.json   源：  8000Hz、0.500s
    rate_mismatch.wav                      16000Hz，用于演示采样率报错
    broken.wav / broken.json               损坏文件，用于演示错误处理
"""

from __future__ import annotations

import json
import sys
import wave
from array import array
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DATA = Path(__file__).resolve().parent / "data"
DATA.mkdir(parents=True, exist_ok=True)


def write_wav(name: str, rate: int, samples, sampwidth: int = 2,
              channels: int = 1) -> Path:
    path = DATA / name
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(rate)
        wf.writeframes(array("h" if sampwidth == 2 else "b", samples).tobytes())
    return path


def write_json(name: str, payload) -> Path:
    path = DATA / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def main() -> None:
    # 目标 8000 帧 @8000Hz = 1.000s，确定性锯齿波，幅度 1000~4995
    target = [1000 + (i % 4000) for i in range(8000)]
    write_wav("target.wav", 8000, target)
    write_json("target_annotations.json", [
        {"time": 0.250, "label": "目标-前段标记"},
        {"time": 0.600, "label": "目标-插入点处标记"},
        {"time": 0.800, "label": "目标-后段标记"},
    ])

    # 源 4000 帧 @8000Hz = 0.500s，负幅度正弦式确定性波形，与目标明显区分
    source = [int(-3000 * (1 - (i % 500) / 500.0)) - 1 for i in range(4000)]
    write_wav("source.wav", 8000, source)
    write_json("source_annotations.json", [
        {"time": 0.000, "label": "源-片段开头"},
        {"time": 0.125, "label": "源-四分之一处"},
        {"time": 0.500, "label": "源-片段结尾"},
    ])

    # 采样率不一致的对照文件
    write_wav("rate_mismatch.wav", 16000, [100] * 8000)

    # 损坏输入
    (DATA / "broken.wav").write_bytes(b"RIFF" + b"\x00not-a-wav" * 32)
    (DATA / "broken.json").write_text("{损坏的 JSON", encoding="utf-8")

    print(f"演示数据已生成于: {DATA}")
    for p in sorted(DATA.iterdir()):
        print(f"  {p.name}  ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
