#!/usr/bin/env python3
"""确定性生成业务样例：若干 WAV 分轨 + 时间表 JSON。

不使用随机数（纯数学函数），重复运行产物逐字节一致，样例可确定性重建。
生成内容覆盖各核对项所需的边界情形：
  dialogue.wav  立体声 44.1kHz 对白模拟信号      1500ms
  sfx.wav       立体声 44.1kHz 高幅度音效          800ms
  ambient.wav   立体声 44.1kHz 环境底噪           1100ms（入点 1000ms，尾部超出声明总时长 100ms）
  mono.wav      单声道 44.1kHz（声道不符样例）      500ms
  sr48k.wav     立体声 48kHz（采样率不符样例）      500ms
  silent.wav    立体声 44.1kHz 全零采样（静音样例） 600ms
  timeline.json 业务时间表（含缺失文件、负入点、越界入点条目）
"""
from __future__ import annotations

import json
import math
import sys
import wave
from array import array
from pathlib import Path

RATE = 44100
OUT_DIR = Path(__file__).resolve().parent.parent / "samples"


def sine_pcm(freq: float, ms: int, amp: float, rate: int = RATE,
             phase: float = 0.0) -> list[int]:
    n = round(ms * rate / 1000)
    return [int(round(amp * 32767 * math.sin(2 * math.pi * freq * i / rate + phase)))
            for i in range(n)]


def write_wav(path: Path, channels_data: list[list[int]], rate: int = RATE) -> None:
    nch = len(channels_data)
    n = len(channels_data[0])
    interleaved = array("h", (channels_data[c][i] for i in range(n) for c in range(nch)))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(nch)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(interleaved.tobytes())
    print(f"生成 {path.name}: {nch}ch {rate}Hz {n}帧")


def mix2(a: list[int], b: list[int], wa: float, wb: float) -> list[int]:
    return [int(round(wa * x + wb * y)) for x, y in zip(a, b)]


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 对白：双正弦叠加模拟人声基频+泛音，左右声道略有差异
    dlg_l = mix2(sine_pcm(220, 1500, 0.40), sine_pcm(330, 1500, 0.15), 1.0, 1.0)
    dlg_r = mix2(sine_pcm(221, 1500, 0.36), sine_pcm(331, 1500, 0.13), 1.0, 1.0)
    write_wav(OUT_DIR / "dialogue.wav", [dlg_l, dlg_r])

    # 音效：880Hz 高幅度脉冲
    sfx = sine_pcm(880, 800, 0.99)
    write_wav(OUT_DIR / "sfx.wav", [sfx, sfx])

    # 环境：低频嗡鸣，长度 1100ms，入点 1000ms → 结束于 2100ms（超出声明 2000ms 共 100ms）
    amb_l = mix2(sine_pcm(110, 1100, 0.20), sine_pcm(55, 1100, 0.10), 1.0, 1.0)
    amb_r = mix2(sine_pcm(110, 1100, 0.18), sine_pcm(55, 1100, 0.09), 1.0, 1.0)
    write_wav(OUT_DIR / "ambient.wav", [amb_l, amb_r])

    # 单声道分轨（核对项 3：声道不符）
    write_wav(OUT_DIR / "mono.wav", [sine_pcm(440, 500, 0.5)])

    # 48kHz 分轨（核对项 3：采样率不符）
    sr48 = sine_pcm(440, 500, 0.5, rate=48000)
    write_wav(OUT_DIR / "sr48k.wav", [sr48, sr48], rate=48000)

    # 全静音分轨（核对项 9）
    write_wav(OUT_DIR / "silent.wav", [[0] * round(600 * RATE / 1000)] * 2)

    timeline = {
        "scene": "cutscene_opening",
        "total_duration_ms": 2000,
        "tracks": [
            {"id": "dialogue", "role": "对白", "file": "dialogue.wav", "in_point_ms": 0},
            {"id": "sfx", "role": "音效", "file": "sfx.wav", "in_point_ms": 600},
            {"id": "ambient", "role": "环境", "file": "ambient.wav", "in_point_ms": 1000},
            {"id": "room_tone", "role": "环境", "file": "silent.wav", "in_point_ms": 0},
            {"id": "narration_mono", "role": "对白", "file": "mono.wav", "in_point_ms": 200},
            {"id": "music_48k", "role": "音效", "file": "sr48k.wav", "in_point_ms": 0},
            {"id": "missing_voice", "role": "对白", "file": "missing.wav", "in_point_ms": 300},
            {"id": "bad_inpoint", "role": "音效", "file": "sfx.wav", "in_point_ms": -50},
            {"id": "late_track", "role": "环境", "file": "sfx.wav", "in_point_ms": 5000},
        ],
    }
    (OUT_DIR / "timeline.json").write_text(
        json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8")
    print("生成 timeline.json")


if __name__ == "__main__":
    sys.exit(main())
