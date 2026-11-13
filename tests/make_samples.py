# -*- coding: utf-8 -*-
"""确定性测试样本生成器（仅标准库，无随机数）。

在独立的 testdata 目录下生成：
  voice_mono.wav   单声道 16 位 PCM，16000 Hz，2000 ms，分 4 个特征区段：
      0-500 ms    正常响度正弦波（幅度 3000，RMS 约 -23.8 dBFS）
      500-1000 ms 全零静音
      1000-1500 ms 极低响度正弦波（幅度 250，RMS 约 -45.3 dBFS，峰值不高）
      1500-2000 ms 接近满幅正弦波（幅度 32000，满幅 90% 以上占比约 28%）
  config_ok.json   6 条片段配置：4 个有效区段 + 2 条非法区间
  config_broken.json       损坏 JSON
  config_missing_field.json 缺必要字段
  voice_stereo.wav         立体声 16 位 PCM（用于格式拒绝测试）

用法：
  python tests/make_samples.py [输出目录，默认 testdata]
"""

from __future__ import annotations

import json
import math
import os
import struct
import sys
import wave

SAMPLE_RATE = 16000
REGION_MS = 500
REGION_FRAMES = SAMPLE_RATE * REGION_MS // 1000  # 8000
REGIONS = [
    ("normal", 3000, 440.0),
    ("silence", 0, 440.0),
    ("quiet", 250, 660.0),
    ("loud", 32000, 880.0),
]


def sine_region(amplitude: int, freq: float) -> list[int]:
    out = []
    for n in range(REGION_FRAMES):
        out.append(int(round(amplitude * math.sin(2 * math.pi * freq * n / SAMPLE_RATE))))
    return out


def write_mono_wav(path: str, samples: list[int]) -> None:
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        for i in range(0, len(samples), 65536):
            block = samples[i:i + 65536]
            wf.writeframes(struct.pack("<%dh" % len(block), *block))


def write_stereo_wav(path: str, frames: int) -> None:
    with wave.open(path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        block = []
        for n in range(frames):
            v = int(round(10000 * math.sin(2 * math.pi * 440 * n / SAMPLE_RATE)))
            block.extend([v, -v])
        wf.writeframes(struct.pack("<%dh" % len(block), *block))


def main(out_dir: str) -> int:
    os.makedirs(out_dir, exist_ok=True)

    all_samples: list[int] = []
    for name, amp, freq in REGIONS:
        region = sine_region(amp, freq) if amp else [0] * REGION_FRAMES
        assert len(region) == REGION_FRAMES
        all_samples.extend(region)

    mono_path = os.path.join(out_dir, "voice_mono.wav")
    write_mono_wav(mono_path, all_samples)

    config = [
        {"id": "seg-normal", "start_ms": 0, "end_ms": 500},
        {"id": "seg-silence", "start_ms": 500, "end_ms": 1000},
        {"id": "seg-quiet", "start_ms": 1000, "end_ms": 1500},
        {"id": "seg-loud", "start_ms": 1500, "end_ms": 2000},
        {"id": "seg-out-of-range", "start_ms": 1800, "end_ms": 2600},
        {"id": "seg-reversed", "start_ms": 1200, "end_ms": 800},
    ]
    with open(os.path.join(out_dir, "config_ok.json"), "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

    with open(os.path.join(out_dir, "config_broken.json"), "w", encoding="utf-8") as f:
        f.write('[{"id": "x", "start_ms": 0, "end_ms": 500}, {broken json,,,]\n')

    with open(
        os.path.join(out_dir, "config_missing_field.json"), "w", encoding="utf-8"
    ) as f:
        json.dump([{"id": "no-time", "start_ms": 0}], f, ensure_ascii=False)

    write_stereo_wav(os.path.join(out_dir, "voice_stereo.wav"), REGION_FRAMES)

    print("已生成测试样本到: %s" % os.path.abspath(out_dir))
    for name in sorted(os.listdir(out_dir)):
        size = os.path.getsize(os.path.join(out_dir, name))
        print("  %-26s %8d bytes" % (name, size))
    return 0


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "testdata"
    sys.exit(main(target))
