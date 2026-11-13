# -*- coding: utf-8 -*-
"""生成确定性本地测试样本（PCM WAV），用于演示与验收。

生成内容（默认输出到 samples/）：
  a.wav       8000 Hz / 单声道 / 16bit，5.0 秒，440 Hz 正弦波
  b.wav       8000 Hz / 单声道 / 16bit，4.0 秒，330 Hz 正弦波
  c.wav       8000 Hz / 单声道 / 16bit，6.0 秒，220 Hz 正弦波
  diff.wav   16000 Hz / 双声道 / 16bit，3.0 秒，880 Hz 正弦波（参数不一致用例）
  corrupt.wav  扩展名为 .wav 的纯文本垃圾文件（损坏用例）

用法：
  python scripts/make_samples.py --out samples
"""

from __future__ import annotations

import argparse
import math
import os
import struct
import wave


def _write_sine_wav(
    path: str,
    framerate: int,
    channels: int,
    duration_sec: float,
    freq_hz: float,
) -> None:
    total = int(round(framerate * duration_sec))
    amp = 16000  # int16 幅度，留余量避免削顶
    with wave.open(path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)  # 16 bit
        wf.setframerate(framerate)
        frames = bytearray()
        for n in range(total):
            value = int(amp * math.sin(2 * math.pi * freq_hz * n / framerate))
            for _ in range(channels):
                frames += struct.pack("<h", value)
        wf.writeframes(bytes(frames))


def make_samples(out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    _write_sine_wav(os.path.join(out_dir, "a.wav"), 8000, 1, 5.0, 440.0)
    _write_sine_wav(os.path.join(out_dir, "b.wav"), 8000, 1, 4.0, 330.0)
    _write_sine_wav(os.path.join(out_dir, "c.wav"), 8000, 1, 6.0, 220.0)
    _write_sine_wav(os.path.join(out_dir, "diff.wav"), 16000, 2, 3.0, 880.0)
    with open(os.path.join(out_dir, "corrupt.wav"), "wb") as fh:
        fh.write(b"NOT-A-RIFF-FILE-binary-garbage-\x00\x01\x02\xff" * 64)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成确定性 WAV 测试样本")
    parser.add_argument("--out", default="samples", help="样本输出目录")
    args = parser.parse_args()
    make_samples(args.out)
    print("样本已生成到目录: %s" % os.path.abspath(args.out))
    for name in sorted(os.listdir(args.out)):
        print("  -", name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
