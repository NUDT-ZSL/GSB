#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
testgen.py -- 确定性测试样本生成器。

所有测试音频/清单/时间轴均由本文件代码确定性生成,不依赖任何外部音频文件。
运行: python testgen.py   (在工作区根目录生成 sample_timeline.json 与 testdata/)
"""

import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine

ROOT = os.path.dirname(os.path.abspath(__file__))
TD = os.path.join(ROOT, "testdata")


def write_pcm(path, samples):
    """samples: 交错的 int 样本(自动饱和到 24bit)。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(engine.encode_s24le([engine.clamp24(int(s)) for s in samples]))


def sine(frames, sr, freq, amp, channels=1):
    out = []
    for n in range(frames):
        v = int(round(amp * math.sin(2.0 * math.pi * freq * n / sr)))
        if channels == 1:
            out.append(v)
        else:
            out.append(v)
            out.append(int(round(0.5 * amp * math.sin(2.0 * math.pi * freq * n / sr + 0.3))))
    return out


def impulse(frames, period, amp, channels=1):
    out = []
    for n in range(frames):
        v = amp if n % period == 0 else 0
        if channels == 1:
            out.append(v)
        else:
            out.append(v)
            out.append(-(v // 2))
    return out


def constant(frames, value, channels=1):
    return [value] * (frames * channels)


def pattern_samples(n):
    """确定性伪随机 24bit 样本(纯整数算术,跨平台一致)。"""
    return [((i * 2654435761 + 12345) & 0xFFFFFF) - 0x800000 for i in range(n)]


def write_pattern_file(path, frames, channels=2):
    """写任意长度的确定性模式文件(用于 ~50MB 流式验证)。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    block_frames = 100000
    block = engine.encode_s24le(pattern_samples(block_frames * channels))
    with open(path, "wb") as f:
        left = frames
        while left > 0:
            n = min(block_frames, left)
            if n == block_frames:
                f.write(block)
            else:
                f.write(engine.encode_s24le(pattern_samples(n * channels)))
            left -= n


def write_sample_timeline(path):
    """内置确定性时间轴:含 1/3 拍、5/7 拍等事件,分段 BPM 含分数。"""
    obj = {
        "segments": [
            {"start": "0", "bpm": "120"},
            {"start": "8", "bpm": "165/2"},
            {"start": "20", "bpm": "99"},
        ],
        "events": [
            {"name": "e1", "position": "1/3"},
            {"name": "e2", "position": "5/7"},
            {"name": "e3", "position": "7"},
            {"name": "e4", "position": "8"},
            {"name": "e5", "position": "29/3"},
            {"name": "e6", "position": "19"},
            {"name": "e7", "position": "25"},
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)


def write_plain_timeline(path, bpm="120"):
    obj = {"segments": [{"start": "0", "bpm": bpm}], "events": []}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)


def main():
    os.makedirs(TD, exist_ok=True)
    write_sample_timeline(os.path.join(ROOT, "sample_timeline.json"))
    write_plain_timeline(os.path.join(TD, "plain_timeline.json"))
    print("generated: sample_timeline.json, testdata/plain_timeline.json")


if __name__ == "__main__":
    main()
