# -*- coding: utf-8 -*-
"""
make_sample.py —— 用标准库确定性生成任务样本（无需网络、无需外部素材）

输出：
  sample.wav       44.1kHz / 16bit / 单声道 PCM，时长约 3 秒。
                   波形由固定数学公式生成（确定性，可重复）：
                     0.0 - 1.0s  220Hz 正弦，幅度 4000
                     1.0 - 2.0s  440Hz 正弦，幅度 12000
                     2.0 - 3.0s  880Hz 正弦，幅度 2000
  edit_list.json   三条示例条目，含 0 dBFS 归一化场景，顺序确定。
"""

from __future__ import annotations

import array
import json
import math
import os
import wave

SAMPLE_RATE = 44100
DURATION_SECONDS = 3
OUT_WAV = "sample.wav"
OUT_JSON = "edit_list.json"


def build_samples() -> array.array:
    total = SAMPLE_RATE * DURATION_SECONDS
    samples = array.array("h", b"\x00\x00" * total)
    for n in range(total):
        t = n / SAMPLE_RATE
        if t < 1.0:
            freq, amp = 220.0, 4000.0
        elif t < 2.0:
            freq, amp = 440.0, 12000.0
        else:
            freq, amp = 880.0, 2000.0
        samples[n] = int(round(amp * math.sin(2.0 * math.pi * freq * t)))
    return samples


def main() -> None:
    samples = build_samples()
    here = os.path.dirname(os.path.abspath(__file__))
    wav_path = os.path.join(here, OUT_WAV)
    json_path = os.path.join(here, OUT_JSON)

    with wave.open(wav_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(samples.tobytes())

    edit_list = [
        {"id": "line_01", "start_ms": 100, "end_ms": 900,
         "target_dbfs": -6.0},
        {"id": "line_02", "start_ms": 1000, "end_ms": 1900,
         "target_dbfs": -3.0},
        {"id": "line_03", "start_ms": 2000, "end_ms": 2950,
         "target_dbfs": 0.0},
    ]
    with open(json_path, "w", encoding="utf-8") as fp:
        json.dump(edit_list, fp, ensure_ascii=False, indent=2)

    print(f"已生成 {wav_path}（{len(samples)} 帧, {SAMPLE_RATE}Hz, "
          f"16bit 单声道）")
    print(f"已生成 {json_path}（{len(edit_list)} 条示例条目）")


if __name__ == "__main__":
    main()
