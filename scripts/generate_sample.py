"""固定种子生成录制棚场景 JSON 与各声源干信号 WAV（确定性、无外部素材）。

用法::

    python scripts/generate_sample.py --root data --seed 20261125

生成内容：

* ``data/scene.json`` 六面体棚、3 个发声点、6 声道扬声器阵列（5.1）；
* ``data/source_*.wav`` 单声道 16bit PCM 干信号，波形由 numpy 默认随机发生器
  按固定种子派生（正弦 + 衰减噪声脉冲，确定性可复现）；
* 目录结构与状态/输出目录占位说明。
"""

from __future__ import annotations

import argparse
import json
import wave
from pathlib import Path

import numpy as np

SEED_DEFAULT = 20261125


def _write_wav(path: Path, samples_int16: np.ndarray, sample_rate: int) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(samples_int16.astype("<i2").tobytes())


def build_dry_signal(rng: np.random.Generator, n_samples: int, sample_rate: int, freqs: tuple[float, ...]) -> np.ndarray:
    t = np.arange(n_samples, dtype=np.float64) / sample_rate
    tone = np.zeros(n_samples, dtype=np.float64)
    for index, freq in enumerate(freqs):
        tone += (0.35 / (index + 1)) * np.sin(2.0 * np.pi * freq * t)
    impulse = np.zeros(n_samples, dtype=np.float64)
    for onset in (0, n_samples // 3, 2 * n_samples // 3):
        length = min(256, n_samples - onset)
        decay = np.exp(-np.linspace(0.0, 6.0, length))
        impulse[onset : onset + length] += 0.4 * decay * rng.standard_normal(length)
    signal = tone + impulse
    peak = float(np.max(np.abs(signal)))
    if peak > 0:
        signal = 0.6 * signal / peak
    return signal


def generate(root: Path, seed: int) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    sample_rate = 48000
    n_samples = 4096
    rng = np.random.default_rng(seed)

    sources = [
        {"id": "src_a", "position": [1.2, 1.0, 1.1], "wav": "src_a.wav",
         "freqs": (220.0, 440.0, 880.0)},
        {"id": "src_b", "position": [3.4, 2.6, 1.8], "wav": "src_b.wav",
         "freqs": (330.0, 660.0)},
        {"id": "src_c", "position": [2.0, 0.8, 2.2], "wav": "src_c.wav",
         "freqs": (180.0, 540.0, 1080.0)},
    ]
    for source in sources:
        signal = build_dry_signal(rng, n_samples, sample_rate, source.pop("freqs"))
        _write_wav(root / source["wav"], np.round(signal * 32767.0).astype(np.int16), sample_rate)

    speakers = [
        {"id": "spk_fl", "position": [0.5, 0.5, 1.2]},
        {"id": "spk_fr", "position": [4.5, 0.5, 1.2]},
        {"id": "spk_c",  "position": [2.5, 0.6, 1.5]},
        {"id": "spk_lfe", "position": [2.5, 0.4, 0.5]},
        {"id": "spk_sl", "position": [0.5, 3.5, 1.2]},
        {"id": "spk_sr", "position": [4.5, 3.5, 1.2]},
    ]

    scene = {
        "seed": seed,
        "room": {"size": [5.0, 4.0, 3.0]},
        "sample_rate": sample_rate,
        "layout": "5.1",
        "reconstruction_order": 2,
        "max_reflections": 3,
        "band_edges_hz": [300.0, 1500.0, 6000.0, 12000.0],
        "band_gains": [1.0, 1.0, 0.9, 0.8, 0.7],
        "absorption": {
            "x-": [0.12, 0.15, 0.22, 0.30, 0.38],
            "x+": [0.10, 0.14, 0.20, 0.28, 0.36],
            "y-": [0.18, 0.22, 0.30, 0.38, 0.45],
            "y+": [0.16, 0.20, 0.28, 0.36, 0.44],
            "z-": [0.25, 0.30, 0.40, 0.50, 0.58],
            "z+": [0.20, 0.26, 0.35, 0.44, 0.52],
        },
        "sources": sources,
        "speakers": speakers,
        "max_workers": 2,
        "memory_budget_bytes": 16 * 1024 * 1024,
        "ir_length_samples": 4096,
    }
    scene_path = root / "scene.json"
    scene_path.write_text(json.dumps(scene, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return {"scene": str(scene_path), "seed": seed, "n_samples": n_samples}


def main() -> None:
    parser = argparse.ArgumentParser(description="生成确定性本地样本")
    parser.add_argument("--root", default="data", help="样本输出目录")
    parser.add_argument("--seed", type=int, default=SEED_DEFAULT)
    args = parser.parse_args()
    result = generate(Path(args.root), args.seed)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
