#!/usr/bin/env python3
"""确定性生成鼓组分轨样例：基准轨 reference.wav、若干分轨 WAV 与描述 tracks.json。

用法: python generate_samples.py [--out samples]
所有样本由固定随机种子生成，重复运行结果一致。每份音频 10 秒（<30s），分轨 5 个（<=8）。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import soundfile as sf

SR = 44100
DUR_S = 10.0
SEED = 20261115

# (名称, 通道数, 真实偏移ms, 期望偏移ms, 基频Hz)
TRACKS = [
    ("kick", 1, 0.0, 0.0, 60.0),
    ("snare", 1, 12.0, 0.0, 200.0),
    ("hihat", 1, 3.0, 3.0, 800.0),
    ("tom", 1, 2.0, 5.0, 120.0),
    ("overhead", 2, 8.0, 0.0, 500.0),
]


def track_signal(rng: np.random.Generator, n: int, sr: int, base_freq: float) -> np.ndarray:
    """生成确定性的打击乐式信号：固定间隔起音 + 指数衰减正弦/噪声。"""
    sig = np.zeros(n)
    n_onsets = int((DUR_S - 0.2) / 0.5)
    onsets = 0.1 + np.arange(n_onsets) * 0.5 + rng.uniform(-0.02, 0.02, size=n_onsets)
    gains = rng.uniform(0.5, 1.0, size=n_onsets)
    for onset, gain in zip(onsets, gains):
        i = int(onset * sr)
        length = min(n - i, int(0.15 * sr))
        if length <= 0:
            continue
        env = np.exp(-np.linspace(0.0, 8.0, length))
        tone = np.sin(2.0 * np.pi * base_freq * np.arange(length) / sr)
        noise = rng.standard_normal(length) * 0.3
        sig[i:i + length] += gain * env * (tone + noise)
    return sig


def main() -> None:
    ap = argparse.ArgumentParser(description="生成确定性鼓组分轨样例")
    ap.add_argument("--out", default="samples", help="样例输出目录")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    rng = np.random.default_rng(SEED)
    n_ref = int(DUR_S * SR)

    # 各分轨“本体”信号（未加偏移）
    bodies = {}
    for name, channels, _true_ms, _exp_ms, freq in TRACKS:
        mono = track_signal(rng, n_ref, SR, freq)
        if channels == 2:
            right = 0.8 * mono + 0.05 * rng.standard_normal(n_ref)
            bodies[name] = np.stack([mono, right], axis=1)
        else:
            bodies[name] = mono

    # 基准轨 = 各分轨本体单声道混合，归一化
    ref = np.zeros(n_ref)
    for name, _ch, _t, _e, _f in TRACKS:
        body = bodies[name]
        ref += body if body.ndim == 1 else body.mean(axis=1)
    ref *= 0.8 / np.max(np.abs(ref))
    sf.write(out / "reference.wav", ref, SR, subtype="PCM_16")

    # 分轨 = 本体延迟真实偏移，长度 = 基准轨长度 + 偏移采样数
    desc_tracks = []
    for name, channels, true_ms, exp_ms, _freq in TRACKS:
        body = bodies[name]
        off = int(round(true_ms * SR / 1000.0))
        total = n_ref + off
        if body.ndim == 1:
            data = np.zeros(total)
            data[off:off + n_ref] = body
        else:
            data = np.zeros((total, channels))
            data[off:off + n_ref, :] = body
        peak = np.max(np.abs(data))
        if peak > 0:
            data *= 0.8 / peak
        sf.write(out / f"{name}.wav", data, SR, subtype="PCM_16")
        desc_tracks.append({
            "name": name,
            "file": f"{name}.wav",
            "channels": channels,
            "expected_offset_ms": exp_ms,
        })

    desc = {
        "reference": "reference.wav",
        "sample_rate": SR,
        "tracks": desc_tracks,
    }
    with open(out / "tracks.json", "w", encoding="utf-8") as f:
        json.dump(desc, f, ensure_ascii=False, indent=2)

    print(f"样例已生成于: {out.resolve()}")
    for t in desc_tracks:
        print(f"  分轨 {t['name']}: 文件={t['file']} 通道={t['channels']} 期望偏移={t['expected_offset_ms']} ms")


if __name__ == "__main__":
    main()
