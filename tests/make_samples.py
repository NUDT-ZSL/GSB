# -*- coding: utf-8 -*-
"""
make_samples.py — 确定性生成验收所需的测试 WAV 与标记 JSON。

不使用随机数，重复运行生成字节级一致的样本。
生成内容（均位于 samples/ 下）：
  stereo_source.wav     44.1kHz / 立体声 / 16-bit，3.0s
                        左声道直流偏移 +1000，右声道静音（用于声道独立性验收）
  mono_source.wav       44.1kHz / 单声道 / 16-bit，3.0s
                        确定性斜坡 + 正弦，用于单声道与小数边界验收
  stereo_levels.wav     44.1kHz / 立体声 / 16-bit，3.0s
                        左右声道均为分段直流电平（便于核验淡化渐变）
  source_8bit.wav       44.1kHz / 单声道 / 8-bit，1.0s（用于格式拒绝验收）
  markers_*.json        各类标记文件
"""

from __future__ import annotations

import json
import math
import os
import sys
import wave

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

SR = 44100
STEREO_SECONDS = 3.0
MONO_SECONDS = 3.0


def write_wav(path, channels, sampwidth, framerate, frames_bytes):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(framerate)
        wf.writeframes(frames_bytes)


def make_stereo_source(path):
    """左声道恒定 +1000（直流偏移），右声道恒定 0（静音）。"""
    n = int(STEREO_SECONDS * SR)
    buf = bytearray()
    for _ in range(n):
        buf += int(1000).to_bytes(2, "little", signed=True)   # 左
        buf += int(0).to_bytes(2, "little", signed=True)      # 右
    write_wav(path, 2, 2, SR, bytes(buf))


def make_mono_source(path):
    """确定性信号：斜坡（-500..500 循环）叠加 440Hz 正弦，全程非零。"""
    n = int(MONO_SECONDS * SR)
    buf = bytearray()
    for i in range(n):
        ramp = (i % 1000) - 500
        tone = int(200 * math.sin(2 * math.pi * 440.0 * i / SR))
        v = max(-32768, min(32767, ramp + tone))
        buf += int(v).to_bytes(2, "little", signed=True)
    write_wav(path, 1, 2, SR, bytes(buf))


def make_stereo_levels(path):
    """分段恒定电平：每 0.5s 一段，电平按段号变化，左右同值。"""
    n = int(STEREO_SECONDS * SR)
    levels = [2000, -2000, 4000, -4000, 8000, -8000]
    seg_len = SR // 2
    buf = bytearray()
    for i in range(n):
        v = levels[min(i // seg_len, len(levels) - 1)]
        buf += int(v).to_bytes(2, "little", signed=True)
        buf += int(v).to_bytes(2, "little", signed=True)
    write_wav(path, 2, 2, SR, bytes(buf))


def make_8bit_source(path):
    """8-bit PCM（无符号），用于验证格式拒绝。"""
    n = SR  # 1.0s
    # 8-bit WAV 为无符号，静音为 128
    buf = bytes([128 + int(20 * math.sin(2 * math.pi * 440.0 * i / SR)) for i in range(n)])
    write_wav(path, 1, 1, SR, buf)


def write_json(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def main(base_dir):
    wav_dir = os.path.join(base_dir, "wav")
    mark_dir = os.path.join(base_dir, "markers")

    stereo = os.path.join(wav_dir, "stereo_source.wav")
    mono = os.path.join(wav_dir, "mono_source.wav")
    levels = os.path.join(wav_dir, "stereo_levels.wav")
    bit8 = os.path.join(wav_dir, "source_8bit.wav")

    make_stereo_source(stereo)
    make_mono_source(mono)
    make_stereo_levels(levels)
    make_8bit_source(bit8)

    # 1) 合法：两段各 0.5s（立体声验收）
    write_json(os.path.join(mark_dir, "markers_two_halves.json"),
               [{"start": 0.5, "end": 1.0}, {"start": 1.5, "end": 2.0}])
    # 2) 非法：start == end
    write_json(os.path.join(mark_dir, "markers_invalid_equal.json"),
               [{"start": 1.5, "end": 1.5}])
    # 3) end 超出音频末尾 0.5s（音频 3.0s，标记到 3.5s）
    write_json(os.path.join(mark_dir, "markers_overflow.json"),
               [{"start": 2.0, "end": 3.5}])
    # 4) 空标记列表
    write_json(os.path.join(mark_dir, "markers_empty.json"), [])
    # 5) 单声道 + 小数边界（0.333s / 1.667s，验证整样本对齐）
    write_json(os.path.join(mark_dir, "markers_fractional.json"),
               [{"start": 0.333, "end": 1.667}])
    # 6) 淡化渐变验收：电平源两段，连接处跨越电平跳变
    write_json(os.path.join(mark_dir, "markers_crossfade.json"),
               [{"start": 0.0, "end": 1.0}, {"start": 1.0, "end": 2.0}])
    # 7) JSON 语法损坏
    bad_path = os.path.join(mark_dir, "markers_bad_json.json")
    os.makedirs(os.path.dirname(os.path.abspath(bad_path)), exist_ok=True)
    with open(bad_path, "w", encoding="utf-8") as f:
        f.write('[{"start": 0.1, "end": 0.2}, {broken json,,,')
    # 8) start 超出音频时长
    write_json(os.path.join(mark_dir, "markers_start_out.json"),
               [{"start": 9.0, "end": 10.0}])

    print("样本生成完毕:")
    for root, _, files in os.walk(base_dir):
        for name in sorted(files):
            p = os.path.join(root, name)
            print("  {} ({} bytes)".format(p, os.path.getsize(p)))


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    main(os.path.join(here, "..", "samples"))
