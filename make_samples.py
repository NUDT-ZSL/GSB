#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""确定性生成测试 WAV 片段（合规 / 规格不符 / 损坏）。

用法: python make_samples.py [输出目录]   （默认 samples/）
全部样本由固定公式生成，无随机数，重复运行结果一致。
"""

import math
import os
import struct
import sys
import wave

RATE = 44100


def write_wav(path, samples, channels=1, width=2, rate=RATE):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        if width == 2:
            if channels == 1:
                w.writeframes(struct.pack("<%dh" % len(samples), *samples))
            else:  # samples 为 (L, R) 元组列表
                flat = [v for pair in samples for v in pair]
                w.writeframes(struct.pack("<%dh" % len(flat), *flat))
        elif width == 1:  # 8bit 无符号
            w.writeframes(bytes(samples))


def sine(freq, seconds, amp, rate=RATE):
    n = int(seconds * rate)
    return [int(round(amp * math.sin(2 * math.pi * freq * i / rate)))
            for i in range(n)]


def silence(seconds, rate=RATE):
    return [0] * int(seconds * rate)


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "samples"
    os.makedirs(out, exist_ok=True)

    # 1) 合规片段：1s 语音 + 2s 静音 + 1s 语音 + 1s 静音 + 1s 语音（共6s）
    ep01 = (sine(440, 1.0, 10000) + silence(2.0) + sine(550, 1.0, 10000)
            + silence(1.0) + sine(660, 1.0, 10000))
    write_wav(os.path.join(out, "ep01_ok.wav"), ep01)

    # 2) 双声道（规格不符）
    stereo = [(s, s) for s in sine(440, 1.0, 8000)]
    write_wav(os.path.join(out, "ep02_stereo.wav"), stereo, channels=2)

    # 3) 8bit（规格不符）
    eight = [int(round(128 + 60 * math.sin(2 * math.pi * 440 * i / RATE)))
             for i in range(RATE)]
    write_wav(os.path.join(out, "ep03_8bit.wav"), eight, width=1)

    # 4) 损坏文件（垃圾字节）
    with open(os.path.join(out, "ep04_corrupt.wav"), "wb") as f:
        f.write(b"NOT-A-REAL-WAV-FILE" + bytes(range(64)))

    # 5) 全静音片段（3s）
    write_wav(os.path.join(out, "ep05_allsilence.wav"), silence(3.0))

    # 6) 接近满幅的恒定电平（32767），用于验证淡出不越界
    write_wav(os.path.join(out, "ep06_fullscale.wav"),
              [32767] * int(2.0 * RATE))

    # 7) 48kHz 采样率（规格不符）
    write_wav(os.path.join(out, "ep07_48k.wav"),
              sine(440, 1.0, 8000, rate=48000), rate=48000)

    # 8) 静音间隙含 -30dB 背景哼声：默认 -40dB 阈值下不算静音，
    #    提到 -20dB 阈值后整段间隙计为静音（用于验证阈值参数生效）
    hum = sine(50, 2.0, 1036)  # 约 -30dBFS
    ep08 = sine(440, 1.0, 10000) + hum + sine(550, 1.0, 10000)
    write_wav(os.path.join(out, "ep08_hum.wav"), ep08)

    print("样本已生成于 %s/:" % out)
    for f in sorted(os.listdir(out)):
        print("  -", f)


if __name__ == "__main__":
    main()
