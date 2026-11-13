#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成确定性测试样本：16 位单声道 WAV、非法格式 WAV 与各类剪辑脚本。"""
import json
import math
import os
import struct
import wave

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")


def write_wav(path, rate, seconds, channels=1, sampwidth=2):
    n = int(rate * seconds)
    frames = bytearray()
    for i in range(n):
        v = int(12000 * math.sin(2 * math.pi * 440 * i / rate))
        if sampwidth == 2:
            b = struct.pack("<h", v)
        else:
            b = struct.pack("<B", ((v >> 8) + 128) & 0xFF)
        frames += b * channels
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(sampwidth)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


def write_script(name, obj):
    path = os.path.join(BASE, "scripts", name)
    if isinstance(obj, str):
        with open(path, "w", encoding="utf-8") as f:
            f.write(obj)
    else:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)


def main():
    os.makedirs(os.path.join(BASE, "scripts"), exist_ok=True)
    # 10 秒 16000 Hz 16 位单声道对白样本（确定性正弦）
    write_wav(os.path.join(BASE, "dialog_16k_10s.wav"), 16000, 10)
    # 非法格式样本
    write_wav(os.path.join(BASE, "stereo.wav"), 16000, 2, channels=2)
    write_wav(os.path.join(BASE, "8bit.wav"), 16000, 2, sampwidth=1)

    write_script("sr_mismatch.json", {"sample_rate": 44100, "intervals": [[1, 2]]})
    write_script("mixed_invalid.json", {"sample_rate": 16000, "intervals": [[3, 1], [5, 8]]})
    write_script("out_of_bounds.json", {"sample_rate": 16000, "intervals": [[-1, 2]]})
    write_script("overlap.json", {"sample_rate": 16000, "intervals": [[1, 4], [3, 6]]})
    write_script("broken.json", "{ 这不是合法 JSON")
    write_script("toplevel_array.json", [[1, 2]])
    write_script("delete_all.json", {"sample_rate": 16000, "intervals": [[0, 10]]})
    write_script("millisecond.json", {"sample_rate": 16000, "intervals": [[0.001, 0.002]]})
    write_script(
        "mixed.json",
        {"sample_rate": 16000, "intervals": [[5, 6], "bad", [1, 2], [9, 99]]},
    )
    write_script("normal.json", {"sample_rate": 16000, "intervals": [[1, 2], [4, 5]]})
    print(f"样本已生成于 {BASE}")


if __name__ == "__main__":
    main()
