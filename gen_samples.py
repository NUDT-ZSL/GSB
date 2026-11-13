# -*- coding: utf-8 -*-
"""gen_samples.py — 生成小规模PCM WAV样本与JSON配置（确定性，无第三方库）。"""
import json
import math
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "samples")


def build_wav(channels, rate, bits, data):
    block_align = channels * (bits // 8)
    fmt = struct.pack("<HHIIHH", 1, channels, rate, rate * block_align,
                      block_align, bits)
    pad = b"\x00" if len(data) & 1 else b""
    riff_size = 4 + (8 + len(fmt)) + (8 + len(data) + len(pad))
    return (b"RIFF" + struct.pack("<I", riff_size) + b"WAVE"
            + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"data" + struct.pack("<I", len(data)) + data + pad)


def quantize(v, bits):
    scale = float(1 << (bits - 1))
    q = int(math.floor(v * scale + 0.5))
    return max(-int(scale), min(int(scale) - 1, q))


def encode(frames, bits):
    """frames: 每帧为各声道浮点元组。"""
    out = bytearray()
    for frame in frames:
        for v in frame:
            q = quantize(v, bits)
            if bits == 8:
                out.append(q + 128)
            elif bits == 16:
                out.extend(struct.pack("<h", q))
            elif bits == 24:
                q &= 0xFFFFFF
                out.extend((q & 0xFF, (q >> 8) & 0xFF, (q >> 16) & 0xFF))
            else:
                out.extend(struct.pack("<i", q))
    return bytes(out)


def voice_like(rate, seconds, channels=1, l2r_ratio=None):
    """类语音信号：基频+谐波，带包络。l2r_ratio 设置时右声道=左声道*ratio。"""
    n = int(rate * seconds)
    frames = []
    for i in range(n):
        t = i / rate
        env = 0.5 + 0.5 * math.sin(2 * math.pi * 2.0 * t)
        f0 = 180.0 + 60.0 * math.sin(2 * math.pi * 1.5 * t)
        v = env * (0.5 * math.sin(2 * math.pi * f0 * t)
                   + 0.25 * math.sin(2 * math.pi * 2 * f0 * t)
                   + 0.125 * math.sin(2 * math.pi * 3 * f0 * t))
        v *= 0.8
        if channels == 1:
            frames.append((v,))
        else:
            r = v * l2r_ratio if l2r_ratio is not None else v * 0.5
            frames.append((v, r))
    return frames


def extreme_frames(rate, seconds, channels=1):
    """极值样本：交替最大/最小合法值，用于饱和测试。"""
    n = int(rate * seconds)
    frames = []
    for i in range(n):
        v = 1.0 if i % 2 == 0 else -1.0
        frames.append(tuple(v for _ in range(channels)))
    return frames


def write(path, payload):
    with open(path, "wb") as f:
        f.write(payload)
    print("生成", path, len(payload), "字节")


def main():
    os.makedirs(OUT, exist_ok=True)

    # 主样本：2秒 44100Hz 16bit 立体声（右=左/2 在整数域精确成立，便于声道关系校验）
    frames = voice_like(44100, 2.0, channels=2)
    left_q = [quantize(f[0], 16) & ~1 for f in frames]  # 强制偶数，半值精确
    right_q = [q // 2 for q in left_q]
    data = bytearray()
    for l, r in zip(left_q, right_q):
        data.extend(struct.pack("<hh", l, r))
    write(os.path.join(OUT, "voice_44k_stereo16.wav"),
          build_wav(2, 44100, 16, bytes(data)))

    # 1秒 16000Hz 16bit 单声道（速度精度测试）
    frames = voice_like(16000, 1.0, channels=1)
    write(os.path.join(OUT, "voice_16k_mono16.wav"),
          build_wav(1, 16000, 16, encode(frames, 16)))

    # 各位深极值样本（0.25秒 8000Hz 单声道）
    for bits in (8, 16, 24, 32):
        frames = extreme_frames(8000, 0.25, channels=1)
        write(os.path.join(OUT, "extreme_8k_mono%d.wav" % bits),
              build_wav(1, 8000, bits, encode(frames, bits)))

    # 非PCM样本：IEEE float (format=3)，用于格式拒绝测试
    rate, channels, bits = 16000, 1, 32
    n = rate // 4
    fdata = b"".join(struct.pack("<f", 0.1 * math.sin(i / 10.0)) for i in range(n))
    block = channels * 4
    fmt = struct.pack("<HHIIHH", 3, channels, rate, rate * block, block, bits)
    riff_size = 4 + (8 + len(fmt)) + (8 + len(fdata))
    payload = (b"RIFF" + struct.pack("<I", riff_size) + b"WAVE"
               + b"fmt " + struct.pack("<I", len(fmt)) + fmt
               + b"data" + struct.pack("<I", len(fdata)) + fdata)
    write(os.path.join(OUT, "float_not_pcm.wav"), payload)

    # 配置样例
    configs = {
        "config_single.json": {"edit": [
            {"start": 0.5, "end": 1.5, "speed": 2.0, "pitch": 2.0}]},
        "config_overlap.json": {"edit": [
            {"start": 0.5, "end": 1.5, "speed": 2.0, "pitch": 0.0},
            {"start": 1.0, "end": 3.0, "speed": 0.5, "pitch": -1.0}]},
        "config_speed2.json": {"edit": [
            {"start": 0.0, "end": 1.0, "speed": 2.0, "pitch": 0.0}]},
        "config_empty.json": {"edit": []},
        "config_missing_speed.json": {"edit": [
            {"start": 0.0, "end": 1.0, "pitch": 0.0}]},
        "config_pitch.json": {"edit": [
            {"start": 0.0, "end": 0.25, "speed": 1.5, "pitch": 7.0}]},
    }
    for name, cfg in configs.items():
        path = os.path.join(OUT, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print("生成", path)


if __name__ == "__main__":
    sys.exit(main())
