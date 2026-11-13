#!/usr/bin/env python3
"""make_samples — 确定性生成验收测试用 WAV 样本(无随机源,可复现)。

用法: python make_samples.py [输出目录]   (默认 samples/)
生成:
  tone5s.wav     5s  44.1kHz 16bit 立体声正弦(L 440Hz / R 554Hz,幅度 0.5)
  loud09.wav     0.5s 44.1kHz 16bit 立体声方波,幅度 0.9(±29490),用于饱和测试
  silence.wav    2s  全零静音
  mono8bit.wav   1s  8bit 单声道(非法格式样本)
  big.wav        60s 44.1kHz 16bit 立体声正弦(约 10.6MB,性能样本)
"""
import math
import os
import sys
import wave
from array import array

SR = 44100
AMP09 = int(round(0.9 * 32767))  # 29490


def write_wav(path, frames: bytes, nchannels=2, sampwidth=2, framerate=SR):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(nchannels)
        w.setsampwidth(sampwidth)
        w.setframerate(framerate)
        w.writeframes(frames)


def stereo(frames_l_r):
    """frames_l_r: 可迭代的 (l, r) 整数样本对 -> interleaved bytes"""
    buf = array("h")
    for l, r in frames_l_r:
        buf.append(l)
        buf.append(r)
    return buf.tobytes()


def gen_tone(path, seconds=5.0, amp=0.5):
    n = int(seconds * SR)
    data = stereo(
        (int(round(amp * 32767 * math.sin(2 * math.pi * 440 * i / SR))),
         int(round(amp * 32767 * math.sin(2 * math.pi * 554 * i / SR))))
        for i in range(n))
    write_wav(path, data)


def gen_loud09(path):
    """方波 ±29490,周期 100 帧,0.5s。"""
    n = int(0.5 * SR)
    data = stereo(((AMP09, AMP09) if (i // 50) % 2 == 0 else (-AMP09, -AMP09))
                  for i in range(n))
    write_wav(path, data)


def gen_silence(path, seconds=2.0):
    write_wav(path, b"\x00" * int(seconds * SR) * 4)


def gen_mono8bit(path):
    n = SR  # 1s
    data = bytes(128 + int(60 * math.sin(2 * math.pi * 440 * i / SR)) for i in range(n))
    write_wav(path, data, nchannels=1, sampwidth=1)


def gen_big(path, seconds=60.0):
    """约 10.6MB 的立体声正弦,分块写入避免一次性占内存。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        chunk = SR  # 1s 一块
        for base in range(0, int(seconds * SR), chunk):
            buf = array("h")
            for i in range(base, min(base + chunk, int(seconds * SR))):
                v = int(round(0.3 * 32767 * math.sin(2 * math.pi * 220 * i / SR)))
                buf.append(v)
                buf.append(v)
            w.writeframes(buf.tobytes())


def main():
    outdir = sys.argv[1] if len(sys.argv) > 1 else "samples"
    jobs = [
        ("tone5s.wav", gen_tone),
        ("loud09.wav", gen_loud09),
        ("silence.wav", gen_silence),
        ("mono8bit.wav", gen_mono8bit),
        ("big.wav", gen_big),
    ]
    for name, fn in jobs:
        path = os.path.join(outdir, name)
        fn(path)
        print("%-14s %10d bytes" % (name, os.path.getsize(path)))


if __name__ == "__main__":
    main()
