#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""确定性构造演示样本：单声道 16 位 PCM WAV + 各类 JSON 剪切剧本。

生成内容（均写入 samples/）：
  interview.wav   6 s 采访录音模拟，分 5 段不同电平/频率的正弦：
                    0.0-1.0s  440 Hz  幅度 0.10
                    1.0-2.5s  550 Hz  幅度 0.30
                    2.5-4.0s  660 Hz  幅度 0.05
                    4.0-5.0s  330 Hz  幅度 0.0005（近乎静音 -> 响度异常）
                    5.0-6.0s  880 Hz  幅度 0.90 （高电平 -> 目标不可达/削顶测试）
  corrupt.wav     损坏的 WAV（随机字节头）
  script_ok.json            全部片段有效
  script_partial.json       含越界、响度异常、目标不可达、损坏源等片段
  script_missing_field.json 片段缺失 target_loudness
  script_bad_time.json      含字符串与负时间
  script_all_invalid.json   全部片段无效
  script_corrupt.json       部分片段引用损坏 WAV
"""
import json
import math
import os
import struct
import wave

SR = 44100
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "samples")

# (起止秒, 频率Hz, 幅度)
SEGMENTS = [
    (0.0, 1.0, 440.0, 0.10),
    (1.0, 2.5, 550.0, 0.30),
    (2.5, 4.0, 660.0, 0.05),
    (4.0, 5.0, 330.0, 0.0005),
    (5.0, 6.0, 880.0, 0.90),
]


def gen_interview(path):
    frames = bytearray()
    for t0, t1, freq, amp in SEGMENTS:
        n = round((t1 - t0) * SR)
        for i in range(n):
            v = int(round(amp * 32767 * math.sin(2 * math.pi * freq * i / SR)))
            frames += struct.pack("<h", max(-32768, min(32767, v)))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(bytes(frames))


def gen_corrupt(path):
    # 确定性的“损坏”内容：看似 RIFF 头但数据非法
    with open(path, "wb") as f:
        f.write(b"RIFF\x00\x01\x00\x00WAVEfmt corrupted-not-a-real-wav")


def write_script(name, obj):
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def main():
    os.makedirs(OUT, exist_ok=True)
    gen_interview(os.path.join(OUT, "interview.wav"))
    gen_corrupt(os.path.join(OUT, "corrupt.wav"))

    # 全部有效
    write_script("script_ok.json", {
        "input": "interview.wav",
        "output": "../out/ok/episode.wav",
        "clips": [
            {"clip": "opening", "start": 0.0, "end": 1.0, "target_loudness": -20.0},
            {"clip": "answer1", "start": 1.0, "end": 2.5, "target_loudness": -18.0},
            {"clip": "answer2", "start": 2.5, "end": 3.5, "target_loudness": -23.0},
        ],
    })

    # 部分有效：越界(1)、响度异常(3)、目标不可达(4)、损坏源(5)
    write_script("script_partial.json", {
        "input": "interview.wav",
        "output": "../out/partial/episode.wav",
        "clips": [
            {"clip": "opening", "start": 0.0, "end": 1.0, "target_loudness": -20.0},
            {"clip": "out_of_range", "start": 5.5, "end": 99.0, "target_loudness": -20.0},
            {"clip": "answer1", "start": 1.0, "end": 2.0, "target_loudness": -18.0},
            {"clip": "near_silence", "start": 4.0, "end": 5.0, "target_loudness": -20.0},
            {"clip": "too_loud_target", "start": 5.0, "end": 6.0, "target_loudness": -0.5},
            {"clip": "broken_source", "start": 0.0, "end": 1.0,
             "target_loudness": -20.0, "source": "corrupt.wav"},
        ],
    })

    # 缺失必填字段
    write_script("script_missing_field.json", {
        "input": "interview.wav",
        "output": "../out/missing/episode.wav",
        "clips": [
            {"clip": "opening", "start": 0.0, "end": 1.0, "target_loudness": -20.0},
            {"clip": "no_target", "start": 1.0, "end": 2.0},
        ],
    })

    # 非法时间（字符串、负值）
    write_script("script_bad_time.json", {
        "input": "interview.wav",
        "output": "../out/badtime/episode.wav",
        "clips": [
            {"clip": "bad_str", "start": "0.5", "end": 1.0, "target_loudness": -20.0},
            {"clip": "bad_neg", "start": -0.5, "end": 1.0, "target_loudness": -20.0},
        ],
    })

    # 全部无效
    write_script("script_all_invalid.json", {
        "input": "interview.wav",
        "output": "../out/allbad/episode.wav",
        "clips": [
            {"clip": "reversed", "start": 2.0, "end": 1.0, "target_loudness": -20.0},
            {"clip": "oob", "start": 7.0, "end": 8.0, "target_loudness": -20.0},
        ],
    })

    # 损坏源 + 正常源混合
    write_script("script_corrupt.json", {
        "input": "interview.wav",
        "output": "../out/corrupt/episode.wav",
        "clips": [
            {"clip": "broken", "start": 0.0, "end": 1.0,
             "target_loudness": -20.0, "source": "corrupt.wav"},
            {"clip": "good1", "start": 0.0, "end": 1.0, "target_loudness": -20.0},
            {"clip": "good2", "start": 1.0, "end": 2.0, "target_loudness": -18.0},
        ],
    })

    print("样本已生成到 %s" % OUT)


if __name__ == "__main__":
    main()
