#!/usr/bin/env python3
"""make_samples.py — 确定性生成本地演示样本(仅标准库,无需外部素材)。

生成内容(默认写入 ./sample_data):
  sample.wav   16 位单声道 PCM,16000Hz,18 秒,含 3 处自然停顿,
               其中 12~13 秒为 -44dBFS 低音量段(用于演示阈值差异)
  clips.json   片段清单,包含正常、重叠停顿、整段落入停顿等情形
  empty.json   空清单(演示 0 秒输出)

用法: python make_samples.py [输出目录]
"""

import json
import math
import os
import struct
import sys
import wave

RATE = 16000
AMP = 12000          # 正常语音段幅度(约 -8.7dBFS)
QUIET_AMP = 200      # 低音量段幅度(约 -44.3dBFS,介于 -50 与 -40 阈值之间)


def tone(freq, seconds, amp=AMP):
    n = int(RATE * seconds)
    return [int(amp * math.sin(2.0 * math.pi * freq * i / RATE))
            for i in range(n)]


def silence(seconds):
    return [0] * int(RATE * seconds)


def build_samples():
    data = []
    data += tone(440, 2.0)          # 0.0 ~ 2.0   讲话
    data += silence(2.0)            # 2.0 ~ 4.0   停顿(2 秒)
    data += tone(550, 5.0)          # 4.0 ~ 9.0   讲话
    data += silence(1.0)            # 9.0 ~ 10.0  停顿(1 秒)
    data += tone(660, 2.0)          # 10.0 ~ 12.0 讲话
    data += tone(660, 1.0, QUIET_AMP)  # 12.0 ~ 13.0 低音量段(默认阈值下记为停顿)
    data += tone(660, 5.0)          # 13.0 ~ 18.0 讲话
    return data


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "sample_data"
    os.makedirs(out_dir, exist_ok=True)

    wav_path = os.path.join(out_dir, "sample.wav")
    samples = build_samples()
    with wave.open(wav_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(RATE)
        wf.writeframes(struct.pack("<%dh" % len(samples), *samples))

    clips = [
        {"name": "intro", "start": 0.2, "end": 1.8},
        {"name": "middle-overlaps-pause", "start": 4.5, "end": 9.2},
        {"name": "lost-in-pause", "start": 2.5, "end": 3.5},
        {"name": "quiet-tail", "start": 11.0, "end": 12.5},
        {"name": "finale", "start": 14.0, "end": 17.5},
    ]
    clips_path = os.path.join(out_dir, "clips.json")
    with open(clips_path, "w", encoding="utf-8") as fh:
        json.dump(clips, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    empty_path = os.path.join(out_dir, "empty.json")
    with open(empty_path, "w", encoding="utf-8") as fh:
        fh.write("[]\n")

    print(f"已生成: {wav_path} ({len(samples) / RATE:.1f} 秒, {RATE}Hz)")
    print(f"已生成: {clips_path}")
    print(f"已生成: {empty_path}")


if __name__ == "__main__":
    main()
