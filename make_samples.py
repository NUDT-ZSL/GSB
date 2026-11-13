#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_samples.py — 用标准库 wave/struct 确定性生成测试 WAV 样本。

生成内容（写入指定目录）：
  sine_gap.wav      单声道 44100Hz：0.75s 正弦 + 0.5s 静音 + 0.75s 正弦（共 2s）
  立体声_音乐.wav    立体声 44100Hz：1s 双声道正弦（含中文文件名）
  全静音.wav         单声道 22050Hz：1s 纯静音（不同采样率）
  损坏_坏头.wav      损坏文件头的伪 WAV
  说明.txt           非 WAV 文件（应被忽略）
"""
import math
import struct
import sys
import wave
from pathlib import Path

AMP = int(0.6 * 32767)  # 正弦波幅值


def sine_samples(freq, seconds, rate, amp=AMP):
    n = int(round(seconds * rate))
    return [int(amp * math.sin(2.0 * math.pi * freq * i / rate)) for i in range(n)]


def silence_samples(seconds, rate):
    return [0] * int(round(seconds * rate))


def write_mono(path, samples, rate):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack("<%dh" % len(samples), *samples))


def write_stereo(path, left, right, rate):
    frames = []
    for l, r in zip(left, right):
        frames.append(l)
        frames.append(r)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack("<%dh" % len(frames), *frames))


def main(out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # 1) 2 秒正弦，中间夹 0.5 秒静音（0.75 + 0.5 + 0.75）
    rate = 44100
    samples = (
        sine_samples(440.0, 0.75, rate)
        + silence_samples(0.5, rate)
        + sine_samples(440.0, 0.75, rate)
    )
    write_mono(out / "sine_gap.wav", samples, rate)

    # 2) 立体声 44100Hz（左右声道不同频率，含中文文件名）
    write_stereo(
        out / "立体声_音乐.wav",
        sine_samples(440.0, 1.0, rate),
        sine_samples(550.0, 1.0, rate),
        rate,
    )

    # 3) 全静音，22050Hz 不同采样率
    write_mono(out / "全静音.wav", silence_samples(1.0, 22050), 22050)

    # 4) 损坏头：看似 RIFF 实则内容残缺
    (out / "损坏_坏头.wav").write_bytes(b"RIFF\x04\x00\x00\x00WAVEbroken-header")

    # 5) 非 WAV 文件，应被忽略
    (out / "说明.txt").write_text("这不是音频文件，应被忽略。", encoding="utf-8")

    print(f"样本已生成于: {out}")
    for p in sorted(out.iterdir()):
        print(f"  {p.name}  ({p.stat().st_size} 字节)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "demo_input")
