#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用标准库 wave 确定性生成测试用单声道 PCM WAV 样本。

全部样本由固定参数的数学函数生成，无随机源、无外部依赖，
同一解释器上重复运行得到逐字节一致的文件。

生成内容 (默认输出到 ./samples):
  source_16k_mono.wav   16kHz 单声道 16bit, 4.0s
                        220Hz 正弦波, 恒定峰值 8000 (小幅度, 便于测高音量钳制)
  square_16k_mono.wav   16kHz 单声道 16bit, 4.0s
                        方波, 恒定值 +5000/-5000 (便于精确核对淡变增益)
  loud_16k_mono.wav     16kHz 单声道 16bit, 1.0s
                        330Hz 正弦波, 峰值 20000 (叠加 200% 音量后必被钳制)
  stereo_16k.wav        16kHz 立体声 16bit, 1.0s (用于声道不兼容验收)
"""

from __future__ import annotations

import array
import math
import os
import sys
import wave

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

SAMPLE_RATE = 16000
SINE_DURATION_S = 4.0
SINE_FREQ_HZ = 220
SINE_PEAK = 8000
SQUARE_LEVEL = 5000
LOUD_DURATION_S = 1.0
LOUD_FREQ_HZ = 330
LOUD_PEAK = 20000
STEREO_DURATION_S = 1.0


def _write_mono(path: str, samples: array.array, framerate: int) -> None:
    wf = wave.open(path, "wb")
    try:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(framerate)
        wf.writeframes(samples.tobytes())
    finally:
        wf.close()


def _write_stereo(path: str, frames: int, framerate: int) -> None:
    # 左声道 +10000，右声道 -10000，交替写小端 16bit
    wf = wave.open(path, "wb")
    try:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(framerate)
        interleaved = array.array("h")
        for _ in range(frames):
            interleaved.append(10000)
            interleaved.append(-10000)
        wf.writeframes(interleaved.tobytes())
    finally:
        wf.close()


def generate(out_dir: str) -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    created: list[str] = []

    # 1) 小幅度正弦波: 8000 * 200% = 16000 不钳制; 另有高音量脚本测 32767 钳制
    sine_frames = int(SAMPLE_RATE * SINE_DURATION_S)
    sine = array.array(
        "h",
        (
            int(round(SINE_PEAK * math.sin(2 * math.pi * SINE_FREQ_HZ * n / SAMPLE_RATE)))
            for n in range(sine_frames)
        ),
    )
    sine_path = os.path.join(out_dir, "source_16k_mono.wav")
    _write_mono(sine_path, sine, SAMPLE_RATE)
    created.append(sine_path)

    # 2) 恒定方波: 样本值恒为 +/-5000，淡变后可精确核对增益序列
    square = array.array("h")
    half_period = SAMPLE_RATE // 440 // 2  # 440Hz 方波, 每半周期翻转
    level = SQUARE_LEVEL
    for n in range(sine_frames):
        if n % half_period == 0:
            level = -level
        square.append(level)
    square_path = os.path.join(out_dir, "square_16k_mono.wav")
    _write_mono(square_path, square, SAMPLE_RATE)
    created.append(square_path)

    # 3) 大幅度正弦波: 峰值 20000，200% 音量后 40000 -> 钳制到 32767
    loud_frames = int(SAMPLE_RATE * LOUD_DURATION_S)
    loud = array.array(
        "h",
        (
            int(round(LOUD_PEAK * math.sin(2 * math.pi * LOUD_FREQ_HZ * n / SAMPLE_RATE)))
            for n in range(loud_frames)
        ),
    )
    loud_path = os.path.join(out_dir, "loud_16k_mono.wav")
    _write_mono(loud_path, loud, SAMPLE_RATE)
    created.append(loud_path)

    # 4) 立体声: 用于格式不兼容验收
    stereo_path = os.path.join(out_dir, "stereo_16k.wav")
    _write_stereo(stereo_path, int(SAMPLE_RATE * STEREO_DURATION_S), SAMPLE_RATE)
    created.append(stereo_path)

    return created


def main() -> int:
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "samples"
    for path in generate(out_dir):
        with wave.open(path, "rb") as wf:
            print(
                f"已生成 {path}: {wf.getnchannels()} 声道, "
                f"{wf.getsampwidth() * 8}bit, {wf.getframerate()}Hz, "
                f"{wf.getnframes()} 帧"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
