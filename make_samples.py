#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""确定性生成演示/验证用 WAV 样本与剪辑清单（仅标准库，总量远小于 10MB）。"""
import json
import math
import os
import struct
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(HERE, "samples")
RATE = 44100


def sine(freq, ms, amp, rate=RATE):
    n = round(ms * rate / 1000.0)
    return [round(amp * math.sin(2 * math.pi * freq * i / rate)) for i in range(n)]


def write_wav(path, samples, rate=RATE, channels=1, sampwidth=2):
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(sampwidth)
        w.setframerate(rate)
        if sampwidth == 2:
            frames = struct.pack("<%dh" % len(samples), *samples)
        else:  # 8 位无符号
            frames = bytes(max(0, min(255, s + 128)) for s in samples)
        w.writeframes(frames)


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def main():
    os.makedirs(SAMPLES, exist_ok=True)

    # 合法单声道 16 位片段：主持人 440Hz / 嘉宾 220Hz，各 3 秒
    write_wav(os.path.join(SAMPLES, "host.wav"), sine(440, 3000, 12000))
    write_wav(os.path.join(SAMPLES, "guest.wav"), sine(220, 3000, 8000))
    # 采样率不同的片段（48kHz）
    write_wav(os.path.join(SAMPLES, "host48k.wav"), sine(440, 1000, 12000, rate=48000), rate=48000)
    # 非单声道 / 非 16 位 / 数据截断 的坏样本
    write_wav(os.path.join(SAMPLES, "stereo.wav"), sine(440, 500, 8000), channels=2)
    write_wav(os.path.join(SAMPLES, "eightbit.wav"), sine(440, 500, 100), sampwidth=1)
    trunc = os.path.join(SAMPLES, "truncated.wav")
    write_wav(trunc, sine(440, 1000, 8000))
    with open(trunc, "r+b") as f:  # 砍掉尾部 40%，制造截断
        size = os.path.getsize(trunc)
        f.truncate(int(size * 0.6))

    M = lambda name: os.path.join(SAMPLES, name)

    # 1) 合法清单：顺序拼接、交叉淡化、+6dB 增益、首尾淡入淡出
    write_json(M("manifest_valid.json"), {
        "version": 1,
        "segments": {"host": "host.wav", "guest": "guest.wav"},
        "fade_in_ms": 20, "fade_out_ms": 50,
        "clips": [
            {"segment": "host", "start_ms": 0, "end_ms": 1000, "gain_db": 0, "fade_ms": 100},
            {"segment": "guest", "start_ms": 500, "end_ms": 1500, "gain_db": 3, "fade_ms": 0},
            {"segment": "host", "start_ms": 1000, "end_ms": 2000, "gain_db": 6},
        ],
    })
    # 2) 时长验收：两段各 1000ms、淡化 100ms → 母带约 1900ms
    write_json(M("manifest_duration.json"), {
        "version": 1,
        "segments": {"host": "host.wav", "guest": "guest.wav"},
        "clips": [
            {"segment": "host", "start_ms": 0, "end_ms": 1000, "fade_ms": 100},
            {"segment": "guest", "start_ms": 0, "end_ms": 1000},
        ],
    })
    # 3) 同一片段不同增益引用两次（0dB 与 -6dB）
    write_json(M("manifest_reuse.json"), {
        "version": 1,
        "segments": {"host": "host.wav"},
        "clips": [
            {"segment": "host", "start_ms": 0, "end_ms": 1000, "gain_db": 0},
            {"segment": "host", "start_ms": 0, "end_ms": 1000, "gain_db": -6},
        ],
    })
    # 4) 钳制验收：+12dB 使 12000 幅度溢出 → 钳到 ±32767/±32768
    write_json(M("manifest_clamp.json"), {
        "version": 1,
        "segments": {"host": "host.wav"},
        "clips": [{"segment": "host", "start_ms": 0, "end_ms": 500, "gain_db": 12}],
    })
    # 5) 三条非法项：片段不存在 / 起点为负 / 终点越界
    write_json(M("manifest_bad_items.json"), {
        "version": 1,
        "segments": {"host": "host.wav"},
        "clips": [
            {"segment": "nobody", "start_ms": 0, "end_ms": 100},
            {"segment": "host", "start_ms": -5, "end_ms": 100},
            {"segment": "host", "start_ms": 0, "end_ms": 99999},
        ],
    })
    # 6) 非法淡化：1000ms 淡化而相邻段仅 500ms
    write_json(M("manifest_bad_fade.json"), {
        "version": 1,
        "segments": {"host": "host.wav", "guest": "guest.wav"},
        "clips": [
            {"segment": "host", "start_ms": 0, "end_ms": 500, "fade_ms": 1000},
            {"segment": "guest", "start_ms": 0, "end_ms": 2000},
        ],
    })
    # 7) 不支持的版本号
    write_json(M("manifest_v99.json"), {
        "version": 99,
        "segments": {"host": "host.wav"},
        "clips": [{"segment": "host", "start_ms": 0, "end_ms": 100}],
    })
    # 8) 采样率不一致
    write_json(M("manifest_badrate.json"), {
        "version": 1,
        "segments": {"host": "host.wav", "host48k": "host48k.wav"},
        "clips": [
            {"segment": "host", "start_ms": 0, "end_ms": 100},
            {"segment": "host48k", "start_ms": 0, "end_ms": 100},
        ],
    })
    # 9) 缺失 clips 字段
    write_json(M("manifest_no_clips.json"), {
        "version": 1, "segments": {"host": "host.wav"},
    })
    # 10) 非法 JSON
    with open(M("manifest_broken.json"), "w", encoding="utf-8") as f:
        f.write('{ "version": 1, "clips": [ oops ] }')
    # 11) 截断 WAV
    write_json(M("manifest_truncated.json"), {
        "version": 1,
        "segments": {"bad": "truncated.wav"},
        "clips": [{"segment": "bad", "start_ms": 0, "end_ms": 100}],
    })
    # 12) 非单声道 / 非 16 位
    write_json(M("manifest_stereo.json"), {
        "version": 1,
        "segments": {"s": "stereo.wav"},
        "clips": [{"segment": "s", "start_ms": 0, "end_ms": 100}],
    })
    write_json(M("manifest_8bit.json"), {
        "version": 1,
        "segments": {"b": "eightbit.wav"},
        "clips": [{"segment": "b", "start_ms": 0, "end_ms": 100}],
    })

    total = sum(os.path.getsize(os.path.join(SAMPLES, f)) for f in os.listdir(SAMPLES))
    print(f"样本与清单已生成于 {SAMPLES}（共 {total} 字节）")


if __name__ == "__main__":
    main()
