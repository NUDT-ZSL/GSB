#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""确定性生成演示用 WAV 素材与素材清单 JSON（无任何随机源，重复运行结果一致）。

用法： python make_samples.py
输出： samples/ 目录下的 WAV 与各场景清单 JSON。
"""
import json
import wave
from pathlib import Path

import numpy as np

SAMPLES = Path(__file__).resolve().parent / "samples"


def write_wav(path: Path, rate: int, channels: int, data_int: np.ndarray) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(np.asarray(data_int, dtype="<i2").tobytes())


def hit_sound(rate: int, channels: int, duration_ms: float, peak: float) -> np.ndarray:
    """确定性合成一段战斗打击音效：多正弦叠加 + 指数衰减包络，峰值精确到 peak。"""
    n = int(round(rate * duration_ms / 1000.0))
    t = np.arange(n, dtype=np.float64) / rate
    env = np.exp(-3.0 * t / (duration_ms / 1000.0))
    cols = []
    for c in range(channels):
        base = 180.0 + 40.0 * c
        sig = (np.sin(2 * np.pi * base * t)
               + 0.6 * np.sin(2 * np.pi * (base * 2.7) * t)
               + 0.3 * np.sin(2 * np.pi * (base * 5.1) * t)) * env
        sig /= np.max(np.abs(sig))
        cols.append(sig)
    sig = np.stack(cols, axis=1) if channels > 1 else cols[0]
    max_int = round(peak * 32768)          # 使实际峰值 = max_int / 32768
    return np.round(sig * max_int).astype(np.int32)


def dump_manifest(name: str, items: list) -> None:
    (SAMPLES / name).write_text(
        json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    SAMPLES.mkdir(exist_ok=True)

    # ---- WAV 素材 ----
    write_wav(SAMPLES / "hit_normal.wav", 48000, 2, hit_sound(48000, 2, 500, 0.5))
    write_wav(SAMPLES / "hit_quiet.wav", 48000, 2, hit_sound(48000, 2, 500, 0.2))
    write_wav(SAMPLES / "hit_long.wav", 48000, 2, hit_sound(48000, 2, 600, 0.5))
    write_wav(SAMPLES / "hit_mono.wav", 48000, 1, hit_sound(48000, 1, 500, 0.2))
    write_wav(SAMPLES / "hit_overlong.wav", 8000, 1, hit_sound(8000, 1, 30500, 0.5))
    (SAMPLES / "corrupt.wav").write_bytes(b"this is not a valid wav file header")

    # ---- 场景清单 ----
    # 主场景：可用 / 峰值偏低需调整 / 时长超范围
    dump_manifest("manifest.json", [
        {"id": "hit_normal", "path": "hit_normal.wav", "target_peak": 0.5,
         "duration_min_ms": 400, "duration_max_ms": 600},
        {"id": "hit_quiet", "path": "hit_quiet.wav", "target_peak": 0.8,
         "duration_min_ms": 400, "duration_max_ms": 600},
        {"id": "hit_long", "path": "hit_long.wav", "target_peak": 0.5,
         "duration_min_ms": 400, "duration_max_ms": 500},
    ])
    # 文件缺失 + 文件头损坏，不阻断其余素材
    dump_manifest("manifest_missing.json", [
        {"id": "hit_normal", "path": "hit_normal.wav", "target_peak": 0.5,
         "duration_min_ms": 400, "duration_max_ms": 600},
        {"id": "ghost", "path": "ghost_deleted.wav", "target_peak": 0.5,
         "duration_min_ms": 400, "duration_max_ms": 600},
        {"id": "corrupt", "path": "corrupt.wav", "target_peak": 0.5,
         "duration_min_ms": 400, "duration_max_ms": 600},
        {"id": "hit_quiet", "path": "hit_quiet.wav", "target_peak": 0.8,
         "duration_min_ms": 400, "duration_max_ms": 600},
    ])
    # 混入单声道素材：格式不一致，不生成试听文件
    dump_manifest("manifest_mono.json", [
        {"id": "hit_normal", "path": "hit_normal.wav", "target_peak": 0.5,
         "duration_min_ms": 400, "duration_max_ms": 600},
        {"id": "hit_quiet", "path": "hit_quiet.wav", "target_peak": 0.8,
         "duration_min_ms": 400, "duration_max_ms": 600},
        {"id": "hit_mono", "path": "hit_mono.wav", "target_peak": 0.8,
         "duration_min_ms": 400, "duration_max_ms": 600},
    ])
    # 目标电平为负 + 时长范围起止颠倒
    dump_manifest("manifest_invalid.json", [
        {"id": "bad_target", "path": "hit_normal.wav", "target_peak": -0.5,
         "duration_min_ms": 400, "duration_max_ms": 600},
        {"id": "bad_range", "path": "hit_normal.wav", "target_peak": 0.5,
         "duration_min_ms": 500, "duration_max_ms": 400},
    ])
    # 空清单
    dump_manifest("manifest_empty.json", [])
    # 单条时长超过 30000ms
    dump_manifest("manifest_toolong.json", [
        {"id": "hit_overlong", "path": "hit_overlong.wav", "target_peak": 0.5,
         "duration_min_ms": 0, "duration_max_ms": 40000},
    ])
    # 单条实际时长超过 30000ms（允许范围本身合法）
    dump_manifest("manifest_toolong_actual.json", [
        {"id": "hit_overlong", "path": "hit_overlong.wav", "target_peak": 0.5,
         "duration_min_ms": 0, "duration_max_ms": 30000},
    ])
    # 目标峰值为满量程：验证试听文件不削波
    dump_manifest("manifest_fullscale.json", [
        {"id": "hit_quiet", "path": "hit_quiet.wav", "target_peak": 1.0,
         "duration_min_ms": 400, "duration_max_ms": 600},
    ])
    # 缺少必需字段
    (SAMPLES / "manifest_missing_field.json").write_text(
        json.dumps([{"id": "no_target", "path": "hit_normal.wav",
                     "duration_min_ms": 400, "duration_max_ms": 600}],
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # JSON 语法损坏
    (SAMPLES / "manifest_broken.json").write_text(
        '[{"id": "x", "path": "hit_normal.wav", \n"target_peak": 0.5,}]\n',
        encoding="utf-8")

    print(f"样例已生成于 {SAMPLES}")


if __name__ == "__main__":
    main()
