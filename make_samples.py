#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成本地确定性测试样本（纯标准库）。

样本内容不是噪声，而是可以耳听/目测校验的确定性波形：
* 轨 0：0.1s 静音 + 3s 全幅方波（交替 ±32767，不会溢出的极值）
* 轨 1：正弦（1000Hz）+ 极值方波段（用于验证限幅）

用法：
    python make_samples.py [OUT_DIR]
"""

from __future__ import annotations

import math
import wave
from array import array
from pathlib import Path
import sys

RATE = 16_000
SAMPLE_WIDTH = 2


def _write(path: Path, samples: array, rate: int = RATE) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(rate)
        wf.setcomptype("NONE", "NONE")
        wf.writeframes(samples.tobytes())


def square_track(duration: float = 3.0, rate: int = RATE) -> array:
    """0.1s 静音，其后为全幅方波（±32767，频率 200Hz）。"""
    samples = array("h")
    lead = int(0.1 * rate)
    samples.extend([0] * lead)
    half_period = rate // 400  # 200Hz
    total = int(duration * rate) - lead
    for i in range(total):
        samples.append(32767 if (i // max(1, half_period)) % 2 == 0 else -32767)
    return samples


def sine_track(duration: float = 2.0, rate: int = RATE,
               freq: float = 1000.0) -> array:
    """1kHz 正弦，幅度 0.9 满幅；最后 0.2s 换成极值方波。"""
    samples = array("h")
    total = int(duration * rate)
    square_start = total - int(0.2 * rate)
    for i in range(total):
        if i < square_start:
            value = 0.9 * 32767 * math.sin(2 * math.pi * freq * i / rate)
        else:
            value = 32767 if (i // max(1, rate // 400)) % 2 == 0 else -32767
        samples.append(int(round(value)))
    return samples


def generate(out_dir: str | Path) -> tuple[Path, Path]:
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    track0 = directory / "track0.wav"
    track1 = directory / "track1.wav"
    _write(track0, square_track())
    _write(track1, sine_track())
    return track0, track1


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "samples"
    t0, t1 = generate(target)
    print(f"已生成: {t0}")
    print(f"已生成: {t1}")
