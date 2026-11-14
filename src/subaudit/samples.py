"""确定性样本构造：纯正弦合成语音 + 固定字幕清单，无随机数。

所有样本由固定参数生成，任何机器、任意次数运行得到的 WAV/JSON 字节一致，
便于离线复现与回归测试。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import soundfile as sf

SAMPLE_RATE = 44100

# (起点ms, 终点ms, 频率Hz, 幅度)
UTTERANCES = [
    (1000.0, 2000.0, 220.0, 0.5),
    (3000.0, 4000.0, 330.0, 0.5),
    (5000.0, 6000.0, 440.0, 0.5),
]
TOTAL_MS = 7000.0

SCENARIOS = ("normal", "early_end", "invalid", "silent", "all")


def _ms_to_samples(ms: float, sample_rate: int) -> int:
    return int(round(ms * sample_rate / 1000.0))


def build_tone_audio(
    utterances=UTTERANCES,
    *,
    total_ms: float = TOTAL_MS,
    sample_rate: int = SAMPLE_RATE,
) -> np.ndarray:
    """按给定区间合成确定相位的正弦波，区间外为纯静音。"""
    total = _ms_to_samples(total_ms, sample_rate)
    audio = np.zeros(total, dtype=np.float64)
    # 全局连续相位，保证每个采样点只由参数决定（确定性）。
    timeline = np.arange(total, dtype=np.float64) / sample_rate
    for start_ms, end_ms, freq, amp in utterances:
        s = _ms_to_samples(start_ms, sample_rate)
        e = _ms_to_samples(end_ms, sample_rate)
        audio[s:e] = amp * np.sin(2.0 * np.pi * freq * timeline[s:e])
    return audio


def build_manifest(scenario: str) -> list[dict]:
    """返回各场景固定的字幕清单。"""
    if scenario == "normal":
        return [
            {"id": "S1", "text": "第一句对白", "start_ms": 800, "end_ms": 2200},
            {"id": "S2", "text": "第二句对白", "start_ms": 2900, "end_ms": 4100},
            {"id": "S3", "text": "第三句对白", "start_ms": 4900, "end_ms": 6100},
        ]
    if scenario == "early_end":
        return [
            {"id": "S1", "text": "第一句对白", "start_ms": 800, "end_ms": 2200},
            # 字幕 3500 结束，人声 4000 结束：字幕漏盖尾部。
            {"id": "S2", "text": "第二句对白", "start_ms": 2900, "end_ms": 3500},
            {"id": "S3", "text": "第三句对白", "start_ms": 4900, "end_ms": 6100},
        ]
    if scenario == "invalid":
        return [
            {"id": "S1", "text": "第一句对白", "start_ms": 800, "end_ms": 2200},
            # 起止倒置，必须被单列且不影响其余句子。
            {"id": "S2", "text": "第二句对白", "start_ms": 4100, "end_ms": 2900},
            {"id": "S3", "text": "第三句对白", "start_ms": 4900, "end_ms": 6100},
        ]
    if scenario == "silent":
        return [
            {"id": "Q1", "text": "静音段第一句", "start_ms": 0, "end_ms": 1500},
            {"id": "Q2", "text": "静音段第二句", "start_ms": 2000, "end_ms": 3500},
        ]
    raise ValueError(f"未知场景: {scenario!r}，可选: {SCENARIOS}")


def make_sample(scenario: str, out_dir: str | Path) -> tuple[Path, Path]:
    """生成单个场景的 WAV 与清单 JSON，返回 (wav路径, json路径)。"""
    if scenario not in SCENARIOS:
        raise ValueError(f"未知场景: {scenario!r}，可选: {SCENARIOS}")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    wav_path = out / f"{scenario}.wav"
    json_path = out / f"{scenario}.json"

    if scenario == "silent":
        audio = np.zeros(_ms_to_samples(4000.0, SAMPLE_RATE), dtype=np.float64)
    else:
        audio = build_tone_audio()

    # PCM_16：标准 fmt+data 块、无 libsndfile 的 PEAK 时间戳块，
    # 保证跨进程重建字节一致；44.1kHz 下边界仍精确到毫秒。
    sf.write(str(wav_path), audio, SAMPLE_RATE, subtype="PCM_16")
    with json_path.open("w", encoding="utf-8", newline="\n") as fp:
        json.dump(build_manifest(scenario), fp, ensure_ascii=False, indent=2)
        fp.write("\n")
    return wav_path, json_path


def make_all_samples(out_dir: str | Path) -> dict[str, tuple[Path, Path]]:
    return {
        scenario: make_sample(scenario, out_dir)
        for scenario in ("normal", "early_end", "invalid", "silent")
    }
